"""
Spectral Dashboard Server

REST API + web dashboard for occupancy monitoring.
"""

import asyncio
import json
import logging
import os

import aiohttp
import asyncpg
import jinja2
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('dashboard')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://cyou:REDACTED_DB_PASS@localhost:5433/cyou')


class Dashboard:
    def __init__(self):
        self.db = None
        self.app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for floor plan uploads
        self.templates = jinja2.Environment(
            loader=jinja2.FileSystemLoader('templates'),
            autoescape=True
        )
        self.templates.policies['json.dumps_function'] = _json_dumps
        self._setup_routes()

    def _setup_routes(self):
        # Pages
        self.app.router.add_get('/', self.page_global)
        self.app.router.add_get('/office/{office_id}', self.page_office)

        # API - Offices
        self.app.router.add_get('/api/offices', self.api_offices)
        self.app.router.add_post('/api/offices', self.api_create_office)
        self.app.router.add_get('/api/offices/{id}', self.api_get_office)
        self.app.router.add_put('/api/offices/{id}', self.api_update_office)
        self.app.router.add_delete('/api/offices/{id}', self.api_delete_office)

        # API - Access Points
        self.app.router.add_get('/api/offices/{office_id}/aps', self.api_office_aps)
        self.app.router.add_post('/api/aps', self.api_create_ap)
        self.app.router.add_put('/api/aps/{id}', self.api_update_ap)
        self.app.router.add_delete('/api/aps/{id}', self.api_delete_ap)

        # API - Occupancy
        self.app.router.add_get('/api/occupancy/global', self.api_global_occupancy)
        self.app.router.add_get('/api/occupancy/office/{office_id}', self.api_office_occupancy)
        self.app.router.add_get('/api/occupancy/ap/{ap_id}', self.api_ap_occupancy)

        # Pages - Who's Here
        self.app.router.add_get('/people/{office_id}', self.page_people)

        # API - Clients (proxy to AP manager)
        self.app.router.add_get('/api/clients/{office_id}', self.api_clients)
        self.app.router.add_get('/api/clients/{office_id}/search', self.api_search_client)
        self.app.router.add_get('/api/clients/{office_id}/first-in', self.api_first_in)
        self.app.router.add_post('/api/clients/static', self.api_toggle_static)

        # API - AP Health & Deployment
        self.app.router.add_get('/api/aps/{id}/health', self.api_ap_health)
        self.app.router.add_post('/api/aps/{id}/deploy', self.api_deploy_listener)
        self.app.router.add_post('/api/aps/{id}/check', self.api_check_listener)
        self.app.router.add_post('/api/aps/{id}/toggle-lock', self.api_toggle_ap_lock)
        self.app.router.add_post('/api/offices/{office_id}/toggle-lock-all', self.api_toggle_lock_all)
        self.app.router.add_post('/api/discover', self.api_discover)

        # API - Baseline & Sensitivity
        self.app.router.add_post('/api/aps/{id}/baseline', self.api_start_baseline)
        self.app.router.add_get('/api/aps/{id}/baseline', self.api_get_baseline)
        self.app.router.add_post('/api/offices/{id}/sensitivity', self.api_set_sensitivity)
        self.app.router.add_get('/api/offices/{id}/sensitivity', self.api_get_sensitivity)
        self.app.router.add_post('/api/offices/{id}/schedule', self.api_set_schedule)
        self.app.router.add_get('/api/offices/{id}/schedule', self.api_get_schedule)

        # Image processing
        self.app.router.add_post('/api/cleanup-image', self.api_cleanup_image)

        # Static files
        self.app.router.add_static('/static', 'static')

    async def start(self):
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
        self.app.on_cleanup.append(lambda _: self.db.close())
        log.info('Dashboard starting on port 8080')
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()
        while True:
            await asyncio.sleep(3600)

    # --- Pages ---

    async def page_global(self, request):
        template = self.templates.get_template('global.html')
        offices = await self.db.fetch(
            'SELECT id, name, location FROM offices ORDER BY name'
        )
        html = template.render(offices=[dict(r) for r in offices])
        return web.Response(text=html, content_type='text/html')

    async def page_office(self, request):
        office_id = int(request.match_info['office_id'])
        template = self.templates.get_template('office.html')

        office = await self.db.fetchrow(
            'SELECT * FROM offices WHERE id = $1', office_id
        )
        if not office:
            raise web.HTTPNotFound()

        aps = await self.db.fetch(
            'SELECT * FROM access_points WHERE office_id = $1 ORDER BY name', office_id
        )

        offices = await self.db.fetch(
            'SELECT id, name FROM offices ORDER BY name'
        )

        server_ip = os.environ.get('SERVER_IP', 'REDACTED_SERVER_IP')
        html = template.render(office=dict(office), aps=[dict(r) for r in aps], offices=[dict(r) for r in offices], server_ip=server_ip)
        return web.Response(text=html, content_type='text/html')

    # --- Office API ---

    async def api_offices(self, request):
        rows = await self.db.fetch('SELECT id, name, location, floor_plan_url FROM offices ORDER BY name')
        return web.json_response([dict(r) for r in rows])

    async def api_create_office(self, request):
        data = await request.json()
        row = await self.db.fetchrow(
            'INSERT INTO offices (name, location) VALUES ($1, $2) RETURNING id, name, location',
            data['name'], data.get('location', '')
        )
        return web.json_response(dict(row), status=201)

    async def api_get_office(self, request):
        office_id = int(request.match_info['id'])
        row = await self.db.fetchrow('SELECT * FROM offices WHERE id = $1', office_id)
        if not row:
            raise web.HTTPNotFound()
        return web.json_response(dict(row), dumps=_json_dumps)

    async def api_update_office(self, request):
        office_id = int(request.match_info['id'])
        data = await request.json()
        await self.db.execute(
            'UPDATE offices SET name = COALESCE($1, name), location = COALESCE($2, location), '
            'floor_plan_url = COALESCE($3, floor_plan_url), '
            'default_ssh_user = COALESCE($4, default_ssh_user), '
            'default_ssh_password = COALESCE($5, default_ssh_password), '
            'timezone = COALESCE($6, timezone) WHERE id = $7',
            data.get('name'), data.get('location'), data.get('floor_plan_url'),
            data.get('default_ssh_user'), data.get('default_ssh_password'),
            data.get('timezone'), office_id
        )
        return web.json_response({'ok': True})

    async def api_delete_office(self, request):
        office_id = int(request.match_info['id'])
        await self.db.execute('DELETE FROM offices WHERE id = $1', office_id)
        return web.json_response({'ok': True})

    # --- AP API ---

    async def api_office_aps(self, request):
        office_id = int(request.match_info['office_id'])
        rows = await self.db.fetch(
            '''SELECT id, name, ip_address, model, map_x, map_y,
                      listener_status, listener_last_seen
               FROM access_points WHERE office_id = $1 ORDER BY name''',
            office_id
        )
        return web.json_response([dict(r) for r in rows], dumps=_json_dumps)

    async def api_create_ap(self, request):
        data = await request.json()
        row = await self.db.fetchrow(
            '''INSERT INTO access_points (office_id, name, ip_address, ssh_user, ssh_password,
                                          mac_address, model, map_x, map_y)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               RETURNING id, name, ip_address''',
            data['office_id'], data['name'], data['ip_address'],
            data['ssh_user'], data['ssh_password'],
            data.get('mac_address'), data.get('model', 'U7 Pro Max'),
            data.get('map_x', 0.5), data.get('map_y', 0.5)
        )
        return web.json_response(dict(row), status=201, dumps=_json_dumps)

    async def api_update_ap(self, request):
        ap_id = int(request.match_info['id'])
        data = await request.json()

        sets = []
        vals = []
        idx = 1
        for field in ['name', 'ip_address', 'ssh_user', 'ssh_password',
                       'mac_address', 'model', 'map_x', 'map_y']:
            if field in data:
                sets.append(f'{field} = ${idx}')
                vals.append(data[field])
                idx += 1

        if sets:
            vals.append(ap_id)
            await self.db.execute(
                f'UPDATE access_points SET {", ".join(sets)} WHERE id = ${idx}',
                *vals
            )
        return web.json_response({'ok': True})

    async def api_delete_ap(self, request):
        ap_id = int(request.match_info['id'])
        await self.db.execute('DELETE FROM access_points WHERE id = $1', ap_id)
        return web.json_response({'ok': True})

    async def page_people(self, request):
        office_id = int(request.match_info['office_id'])
        template = self.templates.get_template('people.html')
        office = await self.db.fetchrow('SELECT * FROM offices WHERE id = $1', office_id)
        if not office:
            raise web.HTTPNotFound()
        offices = await self.db.fetch('SELECT id, name FROM offices ORDER BY name')
        html = template.render(office=dict(office), offices=[dict(r) for r in offices])
        return web.Response(text=html, content_type='text/html')

    async def api_clients(self, request):
        office_id = int(request.match_info['office_id'])

        data, status = await self._proxy_to_manager('GET', f'/api/clients/{office_id}')
        return web.json_response(data, status=status)

    async def api_search_client(self, request):
        office_id = int(request.match_info['office_id'])

        q = request.query.get('q', '')
        data, status = await self._proxy_to_manager('GET', f'/api/clients/{office_id}/search?q={q}')
        return web.json_response(data, status=status)

    async def api_first_in(self, request):
        office_id = int(request.match_info['office_id'])

        data, status = await self._proxy_to_manager('GET', f'/api/clients/{office_id}/first-in')
        return web.json_response(data, status=status)

    async def api_toggle_static(self, request):
        body = await request.json()
        data, status = await self._proxy_to_manager('POST', '/api/clients/static', body)
        return web.json_response(data, status=status)

    # --- Occupancy API ---

    async def api_global_occupancy(self, request):
        """Get occupancy status for all offices from live collector data."""
        offices = await self.db.fetch(
            'SELECT id, name, location FROM offices ORDER BY name'
        )

        # Get live data from collector
        collector_health, _ = await self._proxy_to_collector('GET', '/health')
        per_ap = collector_health.get('per_ap', {}) if collector_health else {}

        # Get AP-to-office mapping
        aps = await self.db.fetch(
            'SELECT id, office_id, ip_address, listener_status FROM access_points'
        )

        result = []
        for office in offices:
            office_aps = [a for a in aps if a['office_id'] == office['id']]
            ap_count = len(office_aps)
            active_count = 0
            total_intensity = 0
            max_intensity = 0

            for ap in office_aps:
                ip = str(ap['ip_address'])
                ap_data = per_ap.get(ip)
                if ap_data and ap_data.get('registered'):
                    active_count += 1
                    ap_intensity = ap_data.get('intensity', 0)
                    total_intensity += ap_intensity
                    max_intensity = max(max_intensity, ap_intensity)

            avg_intensity = total_intensity / active_count if active_count > 0 else 0
            occupied = max_intensity >= 0.15

            result.append({
                'id': office['id'],
                'name': office['name'],
                'location': office['location'],
                'occupied': occupied,
                'avg_intensity': avg_intensity,
                'ap_count': ap_count,
                'active_ap_count': active_count,
            })

        return web.json_response(result, dumps=_json_dumps)

    async def api_office_occupancy(self, request):
        """Get per-AP occupancy for an office with live collector data."""
        office_id = int(request.match_info['office_id'])
        aps = await self.db.fetch('''
            SELECT id, name, ip_address, map_x, map_y, listener_status
            FROM access_points WHERE office_id = $1 ORDER BY name
        ''', office_id)

        # Get live data from collector
        collector_health, _ = await self._proxy_to_collector('GET', '/health')
        per_ap = collector_health.get('per_ap', {}) if collector_health else {}
        last_sample_ago = collector_health.get('last_sample_seconds_ago') if collector_health else None
        collector_status = collector_health.get('status', 'unknown') if collector_health else 'unreachable'

        result = []
        for ap in aps:
            ip = str(ap['ip_address'])
            live = per_ap.get(ip, {})
            result.append({
                'id': ap['id'],
                'name': ap['name'],
                'map_x': ap['map_x'],
                'map_y': ap['map_y'],
                'listener_status': ap['listener_status'],
                'intensity': live.get('intensity', 0),
                'samples': live.get('samples', 0),
                'receiving': live.get('registered', False) and live.get('samples', 0) > 0,
                'last_seen_seconds_ago': live.get('last_seen_seconds_ago'),
            })

        return web.json_response({
            'aps': result,
            'collector': {
                'status': collector_status,
                'last_sample_seconds_ago': last_sample_ago,
            }
        }, dumps=_json_dumps)

    async def api_ap_occupancy(self, request):
        """Get occupancy history for a single AP."""
        ap_id = int(request.match_info['ap_id'])
        limit = int(request.query.get('limit', '100'))
        rows = await self.db.fetch('''
            SELECT time, intensity, radio
            FROM ap_occupancy
            WHERE ap_id = $1
            ORDER BY time DESC LIMIT $2
        ''', ap_id, limit)
        return web.json_response([dict(r) for r in rows], dumps=_json_dumps)

    async def api_ap_health(self, request):
        """Get health log for an AP."""
        ap_id = int(request.match_info['id'])
        rows = await self.db.fetch('''
            SELECT time, status, details
            FROM ap_health_log
            WHERE ap_id = $1
            ORDER BY time DESC LIMIT 50
        ''', ap_id)
        return web.json_response([dict(r) for r in rows], dumps=_json_dumps)

    async def _proxy_to_manager(self, method, path, data=None):
        """Proxy a request to the AP manager service."""
        manager_url = os.environ.get('AP_MANAGER_URL', 'http://ap-manager:8081')
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as session:
                if method == 'POST':
                    async with session.post(f'{manager_url}{path}', json=data) as resp:
                        return await resp.json(), resp.status
                else:
                    async with session.get(f'{manager_url}{path}') as resp:
                        return await resp.json(), resp.status
        except Exception as e:
            return {'status': 'error', 'details': f'AP Manager unreachable: {e}'}, 503

    async def _get_collector_health(self):
        """Get collector health data."""
        collector_url = os.environ.get('COLLECTOR_URL', 'http://collector:8767')
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(f'{collector_url}/health') as resp:
                    return await resp.json()
        except Exception:
            return None

    async def api_check_listener(self, request):
        """Check AP listener + collector data flow."""
        ap_id = int(request.match_info['id'])

        # Get AP status from manager and collector health in parallel
        check_task = self._proxy_to_manager('POST', f'/api/aps/{ap_id}/check')
        collector_task = self._get_collector_health()
        (data, status), collector = await asyncio.gather(check_task, collector_task)

        # Look up this AP's IP to find it in collector stats
        ap = await self.db.fetchrow('SELECT ip_address FROM access_points WHERE id = $1', ap_id)
        if ap and collector and collector.get('per_ap'):
            ip = str(ap['ip_address'])
            ap_flow = collector['per_ap'].get(ip)
            if ap_flow:
                data['collector'] = {
                    'receiving': True,
                    'samples': ap_flow['samples'],
                    'intensity': ap_flow['intensity']
                }
            else:
                data['collector'] = {'receiving': False, 'samples': 0}
        else:
            data['collector'] = {'receiving': False, 'samples': 0}

        return web.json_response(data, status=status)

    async def api_discover(self, request):
        """Proxy discover request to AP manager."""
        body = await request.json()
        data, status = await self._proxy_to_manager('POST', '/api/discover', body)
        return web.json_response(data, status=status)

    async def api_deploy_listener(self, request):
        """Proxy deploy request to AP manager."""
        ap_id = int(request.match_info['id'])
        body = await request.json() if request.can_read_body else {}
        data, status = await self._proxy_to_manager('POST', f'/api/aps/{ap_id}/deploy', body)
        return web.json_response(data, status=status)

    async def api_toggle_ap_lock(self, request):
        """Toggle API token on an AP's listener. Redeploys with or without token."""
        ap_id = int(request.match_info['id'])
        ap = await self.db.fetchrow('SELECT api_token FROM access_points WHERE id = $1', ap_id)
        if not ap:
            raise web.HTTPNotFound()
        # Toggle: if has token, remove it; if no token, add one
        with_token = not bool(ap['api_token'])
        data, status = await self._proxy_to_manager(
            'POST', f'/api/aps/{ap_id}/deploy',
            {'with_token': with_token}
        )
        return web.json_response(data, status=status)

    async def api_toggle_lock_all(self, request):
        """Lock or unlock all APs in an office. If any are unlocked, locks all. If all locked, unlocks all."""
        office_id = int(request.match_info['office_id'])
        aps = await self.db.fetch(
            'SELECT id, api_token FROM access_points WHERE office_id = $1', office_id
        )
        if not aps:
            raise web.HTTPNotFound(text='No APs found')

        locked_count = sum(1 for ap in aps if ap['api_token'])
        # If any unlocked, lock all. If all locked, unlock all.
        with_token = locked_count < len(aps)

        results = {}
        for ap in aps:
            # Skip if already in desired state
            has_token = bool(ap['api_token'])
            if has_token == with_token:
                results[str(ap['id'])] = {'status': 'unchanged'}
                continue
            data, status = await self._proxy_to_manager(
                'POST', f'/api/aps/{ap["id"]}/deploy',
                {'with_token': with_token}
            )
            results[str(ap['id'])] = data

        return web.json_response({
            'action': 'locked' if with_token else 'unlocked',
            'results': results
        })

    async def _proxy_to_collector(self, method, path, data=None):
        """Proxy a request to the collector service."""
        collector_url = os.environ.get('COLLECTOR_URL', 'http://collector:8767')
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                if method == 'POST':
                    async with session.post(f'{collector_url}{path}', json=data) as resp:
                        return await resp.json(), resp.status
                else:
                    async with session.get(f'{collector_url}{path}') as resp:
                        return await resp.json(), resp.status
        except Exception as e:
            return {'status': 'error', 'details': f'Collector unreachable: {e}'}, 503

    async def api_start_baseline(self, request):
        ap_id = int(request.match_info['id'])
        body = await request.json() if request.can_read_body else {}
        data, status = await self._proxy_to_collector('POST', f'/baseline/{ap_id}', body)
        return web.json_response(data, status=status)

    async def api_get_baseline(self, request):
        ap_id = int(request.match_info['id'])
        data, status = await self._proxy_to_collector('GET', f'/baseline/{ap_id}')
        return web.json_response(data, status=status)

    async def api_set_sensitivity(self, request):
        office_id = int(request.match_info['id'])
        body = await request.json()
        data, status = await self._proxy_to_collector('POST', f'/sensitivity/{office_id}', body)
        return web.json_response(data, status=status)

    async def api_get_sensitivity(self, request):
        office_id = int(request.match_info['id'])
        data, status = await self._proxy_to_collector('GET', f'/sensitivity/{office_id}')
        return web.json_response(data, status=status)

    async def api_set_schedule(self, request):
        office_id = int(request.match_info['id'])
        body = await request.json()
        data, status = await self._proxy_to_collector('POST', f'/schedule/{office_id}', body)
        return web.json_response(data, status=status)

    async def api_get_schedule(self, request):
        office_id = int(request.match_info['id'])
        data, status = await self._proxy_to_collector('GET', f'/schedule/{office_id}')
        return web.json_response(data, status=status)

    async def api_cleanup_image(self, request):
        """Clean up a photo of blueprints. All processing is local."""
        import base64
        import cv2
        import numpy as np

        data = await request.json()
        data_url = data.get('image', '')
        mode = data.get('mode', 'blueprint')  # blueprint, photo, high_contrast

        # Decode data URL
        header, b64 = data_url.split(',', 1)
        img_bytes = base64.b64decode(b64)
        img_arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)

        if img is None:
            return web.json_response({'error': 'Invalid image'}, status=400)

        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        if mode == 'blueprint':
            # Best for photos of printed blueprints
            # Denoise
            gray = cv2.fastNlMeansDenoising(gray, h=15)
            # Adaptive threshold - handles uneven lighting from phone photos
            clean = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 10
            )
            # Remove small noise spots
            kernel = np.ones((2, 2), np.uint8)
            clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
            clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, kernel)

        elif mode == 'high_contrast':
            # Heavy cleanup - just major lines
            gray = cv2.fastNlMeansDenoising(gray, h=20)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            clean = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 51, 15
            )
            kernel = np.ones((3, 3), np.uint8)
            clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
            clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, kernel)

        else:
            # Photo mode - lighter touch, preserve more detail
            gray = cv2.fastNlMeansDenoising(gray, h=10)
            clean = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 21, 8
            )

        # Encode back to JPEG data URL
        _, buf = cv2.imencode('.jpg', clean, [cv2.IMWRITE_JPEG_QUALITY, 85])
        result_b64 = base64.b64encode(buf).decode()
        result_url = f'data:image/jpeg;base64,{result_b64}'

        return web.json_response({'image': result_url})


def _json_dumps(obj, **kwargs):
    """JSON serializer that handles datetime, Decimal, etc."""
    import decimal
    from datetime import datetime, date

    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, decimal.Decimal):
            return float(o)
        if hasattr(o, '__str__'):
            return str(o)
        raise TypeError(f'Object of type {type(o)} is not JSON serializable')

    return json.dumps(obj, default=default, **kwargs)


async def main():
    dashboard = Dashboard()
    await dashboard.start()


if __name__ == '__main__':
    asyncio.run(main())
