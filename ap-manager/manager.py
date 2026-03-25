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
        self.app.router.add_post('/api/discover', self.api_discover)
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

        # Background health check loop
        asyncio.create_task(self._health_loop())

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

        # Try HTTP health first
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(f'http://{ip}:{LISTENER_PORT}/status') as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        await self.db.execute(
                            "UPDATE access_points SET listener_status = 'deployed', "
                            "listener_last_seen = NOW(), listener_server_ip = $1 WHERE id = $2",
                            data.get('server_ip'), ap['id']
                        )
                        return {'status': 'installed', 'details': data}
        except Exception:
            pass

        # SSH check
        try:
            rc, stdout, _ = await self._ssh_run(
                ip, user, password,
                'test -x /tmp/spectral_listener && echo binary_exists || echo no_binary'
            )
            if 'binary_exists' in stdout:
                await self.db.execute(
                    "UPDATE access_points SET listener_status = 'stopped' WHERE id = $1", ap['id']
                )
                return {'status': 'stopped', 'details': 'Binary exists but not running'}
            else:
                await self.db.execute(
                    "UPDATE access_points SET listener_status = 'not_installed' WHERE id = $1", ap['id']
                )
                return {'status': 'not_installed', 'details': 'No listener binary found'}
        except Exception as e:
            await self.db.execute(
                "UPDATE access_points SET listener_status = 'unreachable' WHERE id = $1", ap['id']
            )
            return {'status': 'unreachable', 'details': str(e)}

    # --- Deploy listener to AP ---

    async def _deploy_to_ap(self, ap, server_ip=None):
        """Compile, deploy, and start the listener on an AP."""
        if not self.binary_ready:
            await self._compile_binary()
        if not self.binary_ready:
            return {'status': 'error', 'step': 'compile', 'details': 'Compilation failed'}

        ip = str(ap['ip_address'])
        user = ap['ssh_user']
        password = ap['ssh_password']
        target_ip = server_ip or SERVER_IP
        steps = []

        try:
            # Clean slate - kill everything and remove old binary
            steps.append('Cleaning up...')
            await self._ssh_run(ip, user, password,
                'killall spectral_listener 2>/dev/null; sleep 2; '
                'killall -9 spectral_listener 2>/dev/null; '
                'rm -f /tmp/spectral_listener /tmp/spectral.log /tmp/sl.b64; '
                'echo cleaned')
            steps.append('Clean')

            # Deploy binary via base64
            steps.append('Deploying binary...')
            rc, stdout, stderr = await self._ssh_run(
                ip, user, password,
                'cat > /tmp/sl.b64 && base64 -d /tmp/sl.b64 > /tmp/spectral_listener && '
                'chmod +x /tmp/spectral_listener && rm /tmp/sl.b64 && echo deployed',
                stdin_data=self.b64_binary,
                timeout=60
            )
            if 'deployed' not in stdout:
                return {'status': 'error', 'step': 'deploy', 'details': stderr, 'steps': steps}
            steps.append('Binary deployed')

            # Start listener
            steps.append('Starting listener...')
            await self._ssh_run(
                ip, user, password,
                f'nohup /tmp/spectral_listener stream wifi0 17 {target_ip} {COLLECTOR_PORT} '
                f'{LISTENER_PORT} > /tmp/spectral.log 2>&1 & sleep 1 && echo started'
            )
            steps.append('Listener started')

            # Update DB
            await self.db.execute(
                "UPDATE access_points SET listener_status = 'deployed', "
                "listener_last_seen = NOW(), listener_server_ip = $1 WHERE id = $2",
                target_ip, ap['id']
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

        ap = await self.db.fetchrow(
            'SELECT id, ip_address, ssh_user, ssh_password FROM access_points WHERE id = $1', ap_id
        )
        if not ap:
            raise web.HTTPNotFound()
        result = await self._deploy_to_ap(ap, server_ip)
        status_code = 200 if result['status'] == 'deployed' else 500
        return web.json_response(result, status=status_code)

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

    # --- Background health check loop ---

    async def _health_loop(self):
        """Periodically check all APs and auto-remediate."""
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            try:
                aps = await self.db.fetch(
                    'SELECT id, name, ip_address, ssh_user, ssh_password, listener_status '
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
