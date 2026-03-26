"""
Spectral AP Manager

Monitors AP health, deploys/redeploys spectral listeners.
Exposes an HTTP API for the dashboard to trigger on-demand actions.
Cross-compiles the listener binary locally (no external build host).
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import socket

import aiohttp
import asyncpg
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ap-manager')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://cyou:REDACTED_DB_PASS@localhost:5433/cyou')
HEALTH_CHECK_INTERVAL = int(os.environ.get('HEALTH_CHECK_INTERVAL', '60'))
LISTENER_SOURCE = os.environ.get('LISTENER_SOURCE', '/opt/spectral/spectral_listener.c')
LISTENER_BINARY = '/tmp/spectral_listener'
LISTENER_PORT = int(os.environ.get('LISTENER_PORT', '8080'))
COLLECTOR_PORT = int(os.environ.get('COLLECTOR_PORT', '8766'))
SERVER_IP = os.environ.get('SERVER_IP', 'REDACTED_SERVER_IP')
API_PORT = int(os.environ.get('API_PORT', '8081'))


class APManager:
    def __init__(self):
        self.db = None
        self.binary_ready = False
        self.b64_binary = None  # Cached base64-encoded binary
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_post('/api/aps/{id}/check', self.api_check)
        self.app.router.add_post('/api/aps/{id}/deploy', self.api_deploy)
        self.app.router.add_post('/api/offices/{office_id}/deploy', self.api_deploy_office)
        self.app.router.add_post('/api/offices/{office_id}/update', self.api_update_office)
        self.app.router.add_post('/api/discover', self.api_discover)
        self.app.router.add_get('/api/clients/{office_id}', self.api_clients)
        self.app.router.add_get('/api/clients/{office_id}/search', self.api_search_client)
        self.app.router.add_get('/api/clients/{office_id}/first-in', self.api_first_in)
        self.app.router.add_post('/api/clients/static', self.api_toggle_static)
        self.app.router.add_get('/health', self.api_health)

    async def start(self):
        log.info(f'AP Manager starting (server IP: {SERVER_IP})')
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        log.info('Database connected')

        await self._compile_binary()

        # Start HTTP API
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', API_PORT)
        await site.start()
        log.info(f'API listening on port {API_PORT}')

        # Background tasks
        asyncio.create_task(self._health_loop())
        asyncio.create_task(self._client_collector())

        while True:
            await asyncio.sleep(3600)

    # --- Local cross-compilation ---

    async def _compile_binary(self):
        """Cross-compile the spectral listener inside this container."""
        if not os.path.exists(LISTENER_SOURCE):
            log.error(f'Source not found: {LISTENER_SOURCE}')
            return

        log.info('Compiling spectral listener...')
        proc = await asyncio.create_subprocess_exec(
            'arm-linux-gnueabi-gcc', '-O2', '-Wall', '-static',
            '-o', LISTENER_BINARY, LISTENER_SOURCE, '-lpthread',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error(f'Compile failed: {stderr.decode()}')
            return

        # Cache base64 encoding
        with open(LISTENER_BINARY, 'rb') as f:
            self.b64_binary = base64.b64encode(f.read()).decode()

        self.binary_ready = True
        log.info(f'Binary compiled ({len(self.b64_binary)} bytes b64)')

    # --- SSH helpers using sshpass ---

    async def _ssh_run(self, ip, user, password, command, stdin_data=None, timeout=30):
        """Run a command on an AP via sshpass + ssh."""
        args = [
            'sshpass', '-p', password,
            'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
            f'{user}@{ip}', command
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_data else None
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode() if stdin_data else None),
            timeout=timeout
        )
        return proc.returncode, stdout.decode(), stderr.decode()

    # --- Check AP status ---

    async def _check_ap_status(self, ap):
        """Check listener status on an AP. Returns dict with status + details."""
        ip = str(ap['ip_address'])
        user = ap['ssh_user']
        password = ap['ssh_password']
        latency = await self._measure_latency(ip)
        ssh_timeout = max(15, int(latency / 100) * 15)

        # Try HTTP health first
        http_ok = False
        http_data = None
        try:
            headers = {}
            if ap.get('api_token'):
                headers['Authorization'] = f'Bearer {ap["api_token"]}'
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(f'http://{ip}:{LISTENER_PORT}/status', headers=headers) as resp:
                    if resp.status == 200:
                        http_data = await resp.json()
                        http_ok = True
        except Exception:
            pass

        if http_ok:
            await self.db.execute(
                "UPDATE access_points SET listener_status = 'deployed', "
                "listener_last_seen = NOW(), listener_server_ip = $1 WHERE id = $2",
                http_data.get('server_ip'), ap['id']
            )
            # Stale server IP detection — auto-redeploy if pointing to wrong collector
            reported_ip = http_data.get('server_ip', '')
            if reported_ip and reported_ip != SERVER_IP:
                log.warning(f"AP {ap['name']} ({ip}) has stale server IP {reported_ip}, expected {SERVER_IP} — redeploying")
                await self._deploy_to_ap(ap)
                return {'status': 'redeployed', 'details': f'Stale IP {reported_ip} -> {SERVER_IP}'}
            return {'status': 'installed', 'details': http_data}

        # HTTP failed - SSH in and check process + binary directly
        try:
            rc, stdout, _ = await self._ssh_run(
                ip, user, password,
                'echo "BINARY:$(test -x /tmp/spectral_listener && wc -c < /tmp/spectral_listener || echo 0)"; '
                'echo "PROCESS:$(pgrep -f spectral_listener > /dev/null 2>&1 && echo yes || echo no)"; '
                'echo "LOG:$(tail -1 /tmp/spectral.log 2>/dev/null || echo none)"',
                timeout=ssh_timeout
            )

            has_binary = False
            is_running = False
            log_line = ''
            for line in stdout.strip().split('\n'):
                if line.startswith('BINARY:'):
                    size = line.split(':')[1].strip()
                    has_binary = size != '0' and size != ''
                elif line.startswith('PROCESS:'):
                    is_running = 'yes' in line
                elif line.startswith('LOG:'):
                    log_line = line.split(':', 1)[1].strip()

            if is_running:
                # Process is running but HTTP health failed (common on U6-IW)
                await self.db.execute(
                    "UPDATE access_points SET listener_status = 'deployed', "
                    "listener_last_seen = NOW() WHERE id = $1", ap['id']
                )
                return {'status': 'installed', 'details': {
                    'note': 'Running (health API unavailable on this model)',
                    'last_log': log_line
                }}
            elif has_binary:
                await self.db.execute(
                    "UPDATE access_points SET listener_status = 'stopped' WHERE id = $1", ap['id']
                )
                return {'status': 'stopped', 'details': f'Binary exists but not running. Last log: {log_line}'}
            else:
                await self.db.execute(
                    "UPDATE access_points SET listener_status = 'not_installed' WHERE id = $1", ap['id']
                )
                return {'status': 'not_installed', 'details': 'No listener binary found'}
        except asyncio.TimeoutError:
            await self.db.execute(
                "UPDATE access_points SET listener_status = 'unreachable' WHERE id = $1", ap['id']
            )
            return {'status': 'unreachable', 'details': f'SSH timed out ({ssh_timeout}s)'}
        except Exception as e:
            await self.db.execute(
                "UPDATE access_points SET listener_status = 'unreachable' WHERE id = $1", ap['id']
            )
            return {'status': 'unreachable', 'details': str(e)}

    # --- Deploy listener to AP ---

    async def _measure_latency(self, ip):
        """Measure SSH latency to an AP to set appropriate timeouts."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'ping', '-c', '1', '-W', '5', ip,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode()
            # Extract avg from "min/avg/max/stddev = x/y/z/w ms"
            if 'avg' in output:
                parts = output.split('/')
                avg_ms = float(parts[-3])
                return avg_ms
        except Exception:
            pass
        return 100  # default assumption

    async def _deploy_to_ap(self, ap, server_ip=None, with_token=None):
        """Compile, deploy, and start the listener on an AP.
        with_token: True=generate token, False=no token, None=keep existing"""
        if not self.binary_ready:
            await self._compile_binary()
        if not self.binary_ready:
            return {'status': 'error', 'step': 'compile', 'details': 'Compilation failed'}

        ip = str(ap['ip_address'])
        user = ap['ssh_user']
        password = ap['ssh_password']
        target_ip = server_ip or SERVER_IP
        steps = []
        expected_size = len(base64.b64decode(self.b64_binary))

        # Determine API token
        if with_token is True:
            api_token = secrets.token_urlsafe(32)
        elif with_token is False:
            api_token = None
        else:
            api_token = ap.get('api_token')  # keep existing

        try:
            # Step 0: Measure latency and set timeouts
            latency = await self._measure_latency(ip)
            ssh_timeout = max(15, int(latency / 100) * 15)  # Scale with latency
            transfer_timeout = max(60, int(latency / 100) * 120)  # More time for high latency
            steps.append(f'Latency: {int(latency)}ms (ssh timeout: {ssh_timeout}s, transfer: {transfer_timeout}s)')

            # Step 1: Verify SSH connectivity
            steps.append('Testing SSH...')
            try:
                rc, stdout, stderr = await self._ssh_run(ip, user, password, 'echo OK', timeout=ssh_timeout)
                if rc != 0 or 'OK' not in stdout:
                    return {'status': 'error', 'step': 'ssh', 'details': f'SSH failed: rc={rc} {stderr}', 'steps': steps}
            except asyncio.TimeoutError:
                return {'status': 'error', 'step': 'ssh', 'details': f'SSH timed out ({ssh_timeout}s)', 'steps': steps}
            steps.append('SSH OK')

            # Step 2: Check disk space
            steps.append('Checking disk...')
            rc, stdout, _ = await self._ssh_run(ip, user, password,
                "df /tmp | tail -1 | awk '{print $4}'", timeout=ssh_timeout)
            if rc == 0:
                try:
                    avail_kb = int(stdout.strip())
                    needed_kb = (expected_size // 1024) + 1024  # binary + headroom
                    if avail_kb < needed_kb:
                        return {'status': 'error', 'step': 'disk',
                                'details': f'Not enough space: {avail_kb}KB available, need {needed_kb}KB', 'steps': steps}
                    steps.append(f'Disk OK ({avail_kb}KB free)')
                except ValueError:
                    steps.append('Disk check skipped (parse error)')

            # Step 3: Clean slate
            steps.append('Cleaning up...')
            rc, stdout, stderr = await self._ssh_run(ip, user, password,
                'killall spectral_listener 2>/dev/null; sleep 2; '
                'killall -9 spectral_listener 2>/dev/null; '
                'rm -f /tmp/spectral_listener /tmp/spectral.log /tmp/sl.b64; '
                'echo cleaned', timeout=ssh_timeout + 5)
            if 'cleaned' not in stdout:
                return {'status': 'error', 'step': 'clean', 'details': f'Cleanup failed: {stderr}', 'steps': steps}
            steps.append('Clean')

            # Step 4: Transfer binary
            steps.append(f'Transferring binary ({len(self.b64_binary)} bytes b64, timeout {transfer_timeout}s)...')
            try:
                rc, stdout, stderr = await self._ssh_run(
                    ip, user, password,
                    'cat > /tmp/sl.b64 && base64 -d /tmp/sl.b64 > /tmp/spectral_listener && '
                    'chmod +x /tmp/spectral_listener && rm /tmp/sl.b64 && echo transferred',
                    stdin_data=self.b64_binary,
                    timeout=transfer_timeout
                )
                if 'transferred' not in stdout:
                    return {'status': 'error', 'step': 'transfer',
                            'details': f'Transfer failed: {stderr}', 'steps': steps}
            except asyncio.TimeoutError:
                return {'status': 'error', 'step': 'transfer',
                        'details': f'Transfer timed out ({transfer_timeout}s) - high latency link?', 'steps': steps}
            steps.append('Binary transferred')

            # Step 5: Verify binary
            steps.append('Verifying binary...')
            rc, stdout, _ = await self._ssh_run(ip, user, password,
                'test -x /tmp/spectral_listener && wc -c < /tmp/spectral_listener',
                timeout=ssh_timeout)
            if rc != 0:
                return {'status': 'error', 'step': 'verify',
                        'details': 'Binary not found after transfer', 'steps': steps}
            try:
                actual_size = int(stdout.strip())
                if actual_size != expected_size:
                    return {'status': 'error', 'step': 'verify',
                            'details': f'Size mismatch: expected {expected_size}, got {actual_size}',
                            'steps': steps}
                steps.append(f'Binary verified ({actual_size} bytes)')
            except ValueError:
                steps.append('Binary exists (size check skipped)')

            # Step 6: Start listener
            steps.append('Starting listener...')
            token_export = f'export API_TOKEN={api_token}; ' if api_token else ''
            await self._ssh_run(
                ip, user, password,
                f'{token_export}nohup /tmp/spectral_listener stream wifi0 17 {target_ip} {COLLECTOR_PORT} '
                f'{LISTENER_PORT} > /tmp/spectral.log 2>&1 & sleep 2 && echo started',
                timeout=ssh_timeout + 5
            )

            # Step 7: Verify process is running
            steps.append('Verifying process...')
            rc, stdout, _ = await self._ssh_run(ip, user, password,
                'pgrep -f spectral_listener > /dev/null && echo running || echo not_running',
                timeout=ssh_timeout)
            if 'running' not in stdout:
                # Check the log for why it failed
                _, log_out, _ = await self._ssh_run(ip, user, password,
                    'cat /tmp/spectral.log 2>/dev/null | tail -5', timeout=ssh_timeout)
                return {'status': 'error', 'step': 'start',
                        'details': f'Process not running after start. Log: {log_out}', 'steps': steps}
            steps.append('Process running')

            # Update DB
            await self.db.execute(
                "UPDATE access_points SET listener_status = 'deployed', "
                "listener_last_seen = NOW(), listener_server_ip = $1, api_token = $2 WHERE id = $3",
                target_ip, api_token, ap['id']
            )
            await self.db.execute(
                "INSERT INTO ap_health_log (ap_id, status, details) VALUES ($1, 'redeployed', $2)",
                ap['id'], f'Streaming to {target_ip}:{COLLECTOR_PORT}'
            )

            log.info(f"Deployed listener on {ip} -> {target_ip}:{COLLECTOR_PORT}")
            return {'status': 'deployed', 'server_ip': target_ip, 'steps': steps}

        except asyncio.TimeoutError:
            return {'status': 'error', 'step': 'timeout', 'details': 'Operation timed out', 'steps': steps}
        except Exception as e:
            return {'status': 'error', 'step': 'unknown', 'details': str(e), 'steps': steps}

    # --- HTTP API endpoints ---

    async def api_health(self, request):
        return web.json_response({'status': 'ok', 'binary_ready': self.binary_ready})

    async def api_check(self, request):
        ap_id = int(request.match_info['id'])
        ap = await self.db.fetchrow(
            'SELECT id, ip_address, ssh_user, ssh_password FROM access_points WHERE id = $1', ap_id
        )
        if not ap:
            raise web.HTTPNotFound()
        result = await self._check_ap_status(ap)
        return web.json_response(result)

    async def api_deploy(self, request):
        ap_id = int(request.match_info['id'])
        data = await request.json() if request.can_read_body else {}
        server_ip = data.get('server_ip', SERVER_IP)
        with_token = data.get('with_token', None)

        ap = await self.db.fetchrow(
            'SELECT id, ip_address, ssh_user, ssh_password, api_token FROM access_points WHERE id = $1', ap_id
        )
        if not ap:
            raise web.HTTPNotFound()
        result = await self._deploy_to_ap(ap, server_ip, with_token=with_token)
        status_code = 200 if result['status'] == 'deployed' else 500
        return web.json_response(result, status=status_code)

    async def api_deploy_office(self, request):
        """Deploy (fresh) listeners to all APs in an office."""
        office_id = int(request.match_info['office_id'])
        data = await request.json() if request.can_read_body else {}
        server_ip = data.get('server_ip', SERVER_IP)

        aps = await self.db.fetch(
            'SELECT id, name, ip_address, ssh_user, ssh_password FROM access_points WHERE office_id = $1',
            office_id
        )
        if not aps:
            raise web.HTTPNotFound(text='No APs found for this office')

        # Force recompile for fresh deploy
        self.binary_ready = False
        results = {}
        for ap in aps:
            log.info(f"Deploying to {ap['name']} ({ap['ip_address']})")
            result = await self._deploy_to_ap(ap, server_ip)
            results[ap['name']] = result
        return web.json_response(results)

    async def api_update_office(self, request):
        """Update server IP on all running listeners in an office (kill + restart, no recompile)."""
        office_id = int(request.match_info['office_id'])
        data = await request.json() if request.can_read_body else {}
        server_ip = data.get('server_ip', SERVER_IP)

        aps = await self.db.fetch(
            'SELECT id, name, ip_address, ssh_user, ssh_password FROM access_points WHERE office_id = $1',
            office_id
        )
        if not aps:
            raise web.HTTPNotFound(text='No APs found for this office')

        results = {}
        for ap in aps:
            ip = str(ap['ip_address'])
            try:
                # Kill existing listener and restart with new server IP
                await self._ssh_run(
                    ip, ap['ssh_user'], ap['ssh_password'],
                    f'pkill -f spectral_listener; sleep 1; '
                    f'/tmp/spectral_listener stream {server_ip} 8766 > /tmp/spectral.log 2>&1 &',
                    timeout=15
                )
                await self.db.execute(
                    "UPDATE access_points SET listener_server_ip = $1 WHERE id = $2",
                    server_ip, ap['id']
                )
                results[ap['name']] = {'status': 'updated', 'server_ip': server_ip}
                log.info(f"Updated {ap['name']} ({ip}) -> {server_ip}")
            except Exception as e:
                results[ap['name']] = {'status': 'error', 'details': str(e)}
                log.error(f"Failed to update {ap['name']} ({ip}): {e}")
        return web.json_response(results)

    async def api_clients(self, request):
        """Get all clients currently in an office."""
        office_id = int(request.match_info['office_id'])
        rows = await self.db.fetch('''
            SELECT c.mac, c.hostname, c.identity_1x, c.ip_address,
                   c.ssid, c.radio, c.rssi, c.signal, c.uptime,
                   c.last_seen, c.first_seen_today,
                   c.is_static, c.static_label,
                   ap.name as ap_name, ap.id as ap_id
            FROM clients c
            JOIN access_points ap ON c.ap_id = ap.id
            WHERE ap.office_id = $1
              AND c.last_seen > NOW() - INTERVAL '2 minutes'
            ORDER BY c.is_static ASC, c.last_seen DESC
        ''', office_id)
        clients = []
        for r in rows:
            clients.append({
                'mac': r['mac'],
                'hostname': r['hostname'] or '',
                'identity': r['identity_1x'] or '',
                'name': r['identity_1x'] or r['hostname'] or r['mac'],
                'ip': r['ip_address'] or '',
                'ssid': r['ssid'] or '',
                'radio': r['radio'] or '',
                'rssi': r['rssi'],
                'signal': r['signal'],
                'ap_name': r['ap_name'],
                'ap_id': r['ap_id'],
                'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None,
                'first_seen_today': r['first_seen_today'].isoformat() if r['first_seen_today'] else None,
                'uptime': r['uptime'],
                'is_static': r['is_static'],
                'static_label': r['static_label'] or '',
            })
        active = [c for c in clients if not c['is_static']]
        static = [c for c in clients if c['is_static']]
        return web.json_response({'clients': active, 'static': static, 'count': len(active), 'static_count': len(static)})

    async def api_search_client(self, request):
        """Search for a client by name/hostname/identity."""
        office_id = int(request.match_info['office_id'])
        q = request.query.get('q', '').strip()
        if not q:
            return web.json_response({'results': []})

        rows = await self.db.fetch('''
            SELECT c.mac, c.hostname, c.identity_1x, c.ip_address,
                   c.last_seen, c.first_seen_today,
                   ap.name as ap_name
            FROM clients c
            JOIN access_points ap ON c.ap_id = ap.id
            WHERE ap.office_id = $1
              AND (c.hostname ILIKE $2 OR c.identity_1x ILIKE $2 OR c.mac ILIKE $2)
              AND c.last_seen > NOW() - INTERVAL '24 hours'
            ORDER BY c.last_seen DESC
        ''', office_id, f'%{q}%')
        results = [{
            'mac': r['mac'],
            'name': r['identity_1x'] or r['hostname'] or r['mac'],
            'hostname': r['hostname'] or '',
            'identity': r['identity_1x'] or '',
            'ip': r['ip_address'] or '',
            'ap_name': r['ap_name'],
            'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None,
            'here_now': r['last_seen'].timestamp() > (__import__('time').time() - 120) if r['last_seen'] else False,
        } for r in rows]
        return web.json_response({'results': results, 'query': q})

    async def api_first_in(self, request):
        """Get first-in/last-out for today. Excludes static devices."""
        office_id = int(request.match_info['office_id'])
        rows = await self.db.fetch('''
            SELECT c.mac, c.hostname, c.identity_1x, c.first_seen_today,
                   c.last_seen, c.is_static, ap.name as ap_name
            FROM clients c
            JOIN access_points ap ON c.ap_id = ap.id
            WHERE ap.office_id = $1
              AND c.first_seen_today::date = CURRENT_DATE
              AND c.is_static = FALSE
            ORDER BY c.first_seen_today ASC
        ''', office_id)
        people = []
        seen_names = set()
        for r in rows:
            name = r['identity_1x'] or r['hostname'] or r['mac']
            if name in seen_names:
                continue
            seen_names.add(name)
            here_now = r['last_seen'].timestamp() > (__import__('time').time() - 120) if r['last_seen'] else False
            people.append({
                'name': name,
                'hostname': r['hostname'] or '',
                'identity': r['identity_1x'] or '',
                'first_seen': r['first_seen_today'].isoformat() if r['first_seen_today'] else None,
                'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None,
                'ap_name': r['ap_name'],
                'here_now': here_now,
            })
        return web.json_response({'people': people, 'count': len(people)})

    async def api_toggle_static(self, request):
        """Mark/unmark a client as a static device."""
        data = await request.json()
        mac = data.get('mac', '')
        is_static = data.get('is_static', True)
        label = data.get('label', '')
        await self.db.execute(
            'UPDATE clients SET is_static = $1, static_label = $2 WHERE mac = $3',
            is_static, label if label else None, mac
        )
        return web.json_response({'ok': True, 'mac': mac, 'is_static': is_static})

    async def api_discover(self, request):
        """Scan an IP range for UniFi APs."""
        data = await request.json()
        subnet = data.get('subnet', '')  # e.g. "10.68.30.0/24"
        ssh_user = data.get('ssh_user', '')
        ssh_password = data.get('ssh_password', '')

        if not subnet or not ssh_user or not ssh_password:
            return web.json_response({'error': 'Need subnet, ssh_user, ssh_password'}, status=400)

        # Parse subnet
        import ipaddress
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            return web.json_response({'error': f'Invalid subnet: {e}'}, status=400)

        # Get already-registered APs
        registered = await self.db.fetch('SELECT ip_address FROM access_points')
        registered_ips = {str(r['ip_address']) for r in registered}

        # Scan hosts concurrently (skip network and broadcast)
        hosts = [str(ip) for ip in network.hosts()]
        log.info(f'Discovering APs on {subnet} ({len(hosts)} hosts)')

        results = []
        semaphore = asyncio.Semaphore(20)  # Max 20 concurrent probes

        async def probe(ip):
            async with semaphore:
                try:
                    rc, stdout, _ = await self._ssh_run(
                        ip, ssh_user, ssh_password,
                        'cat /proc/sys/kernel/hostname; '
                        'test -f /usr/sbin/spectraltool && echo "spectral:yes" || echo "spectral:no"; '
                        'lsmod 2>/dev/null | grep -q qca_spectral && echo "module:yes" || echo "module:no"; '
                        'iwconfig 2>/dev/null | grep -c IEEE || echo "0"',
                        timeout=8
                    )
                    if rc == 0:
                        lines = stdout.strip().split('\n')
                        hostname = lines[0] if lines else ip
                        has_spectral = any('spectral:yes' in l for l in lines)
                        has_module = any('module:yes' in l for l in lines)
                        return {
                            'ip': ip,
                            'hostname': hostname,
                            'has_spectral': has_spectral,
                            'has_module': has_module,
                            'reachable': True,
                            'already_registered': ip in registered_ips
                        }
                except Exception:
                    pass
                return None

        tasks = [probe(ip) for ip in hosts]
        probe_results = await asyncio.gather(*tasks)
        results = [r for r in probe_results if r is not None]

        log.info(f'Discovered {len(results)} APs on {subnet}')
        return web.json_response({'found': results, 'scanned': len(hosts)})

    # --- Client collection ---

    async def _client_collector(self):
        """Periodically poll mca-dump from all APs to get client data."""
        await asyncio.sleep(10)  # Let things settle
        while True:
            try:
                aps = await self.db.fetch(
                    "SELECT id, name, ip_address, ssh_user, ssh_password "
                    "FROM access_points WHERE listener_status = 'deployed'"
                )
                for ap in aps:
                    await self._collect_clients(ap)
            except Exception as e:
                log.error(f'Client collection error: {e}')
            await asyncio.sleep(30)  # Poll every 30 seconds

    async def _collect_clients(self, ap):
        """SSH into AP, run mca-dump, extract client data."""
        ip = str(ap['ip_address'])
        ap_id = ap['id']

        try:
            rc, stdout, _ = await self._ssh_run(
                ip, ap['ssh_user'], ap['ssh_password'],
                'mca-dump 2>/dev/null',
                timeout=15
            )
            if rc != 0 or not stdout.strip():
                return

            import json as _json
            dump = _json.loads(stdout)

            seen_macs = set()
            now = 'NOW()'

            for vap in dump.get('vap_table', []):
                ssid = vap.get('essid', '')
                radio = vap.get('radio', '')
                for sta in vap.get('sta_table', []):
                    mac = sta.get('mac', '')
                    if not mac:
                        continue

                    seen_macs.add(mac)
                    hostname = sta.get('hostname', '')
                    identity = sta.get('1x_identity', '')
                    client_ip = sta.get('ip', '')
                    rssi = sta.get('rssi', 0)
                    signal = sta.get('signal', 0)
                    uptime = sta.get('uptime', 0)

                    # Upsert client record
                    existing = await self.db.fetchrow(
                        'SELECT id, first_seen_today FROM clients WHERE mac = $1 AND ap_id = $2',
                        mac, ap_id
                    )

                    if existing:
                        # Update existing
                        await self.db.execute('''
                            UPDATE clients SET
                                hostname = COALESCE(NULLIF($1, ''), hostname),
                                identity_1x = COALESCE(NULLIF($2, ''), identity_1x),
                                ip_address = COALESCE(NULLIF($3, ''), ip_address),
                                ssid = $4, radio = $5, rssi = $6, signal = $7,
                                uptime = $8, last_seen = NOW()
                            WHERE mac = $9 AND ap_id = $10
                        ''', hostname, identity, client_ip, ssid, radio,
                            rssi, signal, uptime, mac, ap_id)

                        # Reset first_seen_today if it's a new day
                        if existing['first_seen_today'].date() < __import__('datetime').date.today():
                            await self.db.execute(
                                'UPDATE clients SET first_seen_today = NOW() WHERE id = $1',
                                existing['id']
                            )
                    else:
                        # New client on this AP
                        await self.db.execute('''
                            INSERT INTO clients (mac, hostname, identity_1x, ip_address,
                                                 ap_id, ssid, radio, rssi, signal, uptime)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ''', mac, hostname, identity, client_ip,
                            ap_id, ssid, radio, rssi, signal, uptime)

                        # Log arrival event
                        await self.db.execute('''
                            INSERT INTO client_events (mac, hostname, identity_1x, ap_id, event_type)
                            VALUES ($1, $2, $3, $4, 'arrived')
                        ''', mac, hostname, identity, ap_id)

            # Auto-mark devices with 24h+ uptime as static
            await self.db.execute('''
                UPDATE clients SET is_static = TRUE
                WHERE ap_id = $1 AND uptime > 86400 AND is_static = FALSE
            ''', ap_id)

            # Check for departures - clients we knew about but aren't in this dump
            known = await self.db.fetch(
                "SELECT mac, hostname, identity_1x FROM clients WHERE ap_id = $1 AND last_seen > NOW() - INTERVAL '5 minutes'",
                ap_id
            )
            for row in known:
                if row['mac'] not in seen_macs:
                    # Client departed
                    await self.db.execute('''
                        INSERT INTO client_events (mac, hostname, identity_1x, ap_id, event_type)
                        VALUES ($1, $2, $3, $4, 'departed')
                    ''', row['mac'], row['hostname'], row['identity_1x'], ap_id)

        except Exception as e:
            log.debug(f'Client collection from {ip} failed: {e}')

    # --- Background health check loop ---

    async def _health_loop(self):
        """Periodically check all APs and auto-remediate."""
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            try:
                aps = await self.db.fetch(
                    'SELECT id, name, ip_address, ssh_user, ssh_password, listener_status, api_token '
                    'FROM access_points'
                )
                for ap in aps:
                    result = await self._check_ap_status(ap)
                    log.debug(f"AP {ap['name']} ({ap['ip_address']}): {result['status']}")
            except Exception as e:
                log.error(f'Health check error: {e}')


async def main():
    manager = APManager()
    await manager.start()


if __name__ == '__main__':
    asyncio.run(main())
