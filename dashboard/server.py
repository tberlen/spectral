"""
C-You Dashboard Server

REST API + web dashboard for occupancy monitoring.
"""

import asyncio
import json
import logging
import os

import asyncpg
import jinja2
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('dashboard')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://cyou:REDACTED_DB_PASS@localhost:5433/cyou')


class Dashboard:
    def __init__(self):
        self.db = None
        self.app = web.Application()
        self.templates = jinja2.Environment(
            loader=jinja2.FileSystemLoader('templates'),
            autoescape=True
        )
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

        # API - AP Health
        self.app.router.add_get('/api/aps/{id}/health', self.api_ap_health)

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
        html = template.render(offices=offices)
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

        html = template.render(office=office, aps=aps, offices=offices)
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
            'floor_plan_url = COALESCE($3, floor_plan_url) WHERE id = $4',
            data.get('name'), data.get('location'), data.get('floor_plan_url'), office_id
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

    # --- Occupancy API ---

    async def api_global_occupancy(self, request):
        """Get occupancy status for all offices."""
        rows = await self.db.fetch('''
            SELECT DISTINCT ON (o.id)
                o.id, o.name, o.location,
                oo.occupied, oo.avg_intensity, oo.ap_count, oo.active_ap_count,
                oo.time as last_update
            FROM offices o
            LEFT JOIN office_occupancy oo ON o.id = oo.office_id
            ORDER BY o.id, oo.time DESC
        ''')
        return web.json_response([dict(r) for r in rows], dumps=_json_dumps)

    async def api_office_occupancy(self, request):
        """Get per-AP occupancy for an office."""
        office_id = int(request.match_info['office_id'])
        rows = await self.db.fetch('''
            SELECT DISTINCT ON (ap.id)
                ap.id, ap.name, ap.map_x, ap.map_y, ap.listener_status,
                ao.intensity, ao.radio, ao.time as last_update
            FROM access_points ap
            LEFT JOIN ap_occupancy ao ON ap.id = ao.ap_id
            WHERE ap.office_id = $1
            ORDER BY ap.id, ao.time DESC
        ''', office_id)
        return web.json_response([dict(r) for r in rows], dumps=_json_dumps)

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


def _json_dumps(obj):
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

    return json.dumps(obj, default=default)


async def main():
    dashboard = Dashboard()
    await dashboard.start()


if __name__ == '__main__':
    asyncio.run(main())
