"""
C-You AP Manager

Monitors AP health, deploys/redeploys spectral listeners automatically.
Handles AP reboots, stale server IPs, and listener failures.
"""

import asyncio
import json
import logging
import os
import socket
import subprocess
import tempfile

import aiohttp
import asyncpg
import asyncssh

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ap-manager')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://cyou:REDACTED_DB_PASS@localhost:5433/cyou')
BUILD_HOST = os.environ.get('BUILD_HOST', 'REDACTED_BUILD_SERVER')
BUILD_USER = os.environ.get('BUILD_USER', 'root')
HEALTH_CHECK_INTERVAL = int(os.environ.get('HEALTH_CHECK_INTERVAL', '60'))
LISTENER_SOURCE = os.environ.get('LISTENER_SOURCE', '/opt/spectral/spectral_listener.c')
LISTENER_BINARY = os.environ.get('LISTENER_BINARY', '/opt/listener/spectral_listener')
LISTENER_PORT = int(os.environ.get('LISTENER_PORT', '8080'))
COLLECTOR_PORT = int(os.environ.get('COLLECTOR_PORT', '8766'))


def get_my_ip():
    """Get this server's IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


class APManager:
    def __init__(self):
        self.db = None
        self.server_ip = get_my_ip()
        self.binary_ready = False

    async def start(self):
        log.info(f'AP Manager starting (server IP: {self.server_ip})')
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        log.info('Database connected')

        # Ensure we have a compiled binary
        await self._ensure_binary()

        # Main health check loop
        while True:
            try:
                await self._check_all_aps()
            except Exception as e:
                log.error(f'Health check cycle error: {e}')
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    async def _ensure_binary(self):
        """Ensure the spectral listener binary is compiled and ready."""
        if os.path.exists(LISTENER_BINARY):
            log.info(f'Listener binary found at {LISTENER_BINARY}')
            self.binary_ready = True
            return

        log.info('Compiling spectral listener binary...')
        try:
            await self._compile_binary()
            self.binary_ready = True
            log.info('Binary compiled successfully')
        except Exception as e:
            log.error(f'Failed to compile binary: {e}')
            self.binary_ready = False

    async def _compile_binary(self):
        """Cross-compile on the build host."""
        proc = await asyncio.create_subprocess_exec(
            'scp', LISTENER_SOURCE, f'{BUILD_USER}@{BUILD_HOST}:/tmp/spectral_listener.c',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()

        proc = await asyncio.create_subprocess_exec(
            'ssh', f'{BUILD_USER}@{BUILD_HOST}',
            'arm-linux-gnueabi-gcc -O2 -Wall -static -o /tmp/spectral_listener /tmp/spectral_listener.c',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f'Compile failed: {stderr.decode()}')

        # Fetch binary
        os.makedirs(os.path.dirname(LISTENER_BINARY), exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            'scp', f'{BUILD_USER}@{BUILD_HOST}:/tmp/spectral_listener', LISTENER_BINARY,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()

    async def _check_all_aps(self):
        """Check health of all registered APs."""
        aps = await self.db.fetch(
            'SELECT id, name, ip_address, ssh_user, ssh_password, listener_status '
            'FROM access_points'
        )

        if not aps:
            log.debug('No APs registered')
            return

        tasks = [self._check_ap(ap) for ap in aps]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_ap(self, ap):
        """Check a single AP's health and remediate if needed."""
        ap_id = ap['id']
        ip = str(ap['ip_address'])
        name = ap['name']

        # Step 1: Try HTTP health check
        status = await self._http_health_check(ip)

        if status == 'healthy':
            # Check if server IP is current
            server_ip = await self._get_ap_server_ip(ip)
            if server_ip and server_ip != self.server_ip:
                log.info(f'AP {name} ({ip}): stale server IP {server_ip}, redeploying')
                await self._deploy_listener(ap)
                await self._log_health(ap_id, 'redeployed', f'Stale server IP: {server_ip}')
                return

            await self._update_ap_status(ap_id, 'deployed')
            return

        # Step 2: HTTP failed - try SSH to check/fix
        log.info(f'AP {name} ({ip}): health check failed ({status}), attempting SSH remediation')

        ssh_ok = await self._ssh_check_and_fix(ap)
        if ssh_ok:
            await self._log_health(ap_id, 'redeployed', 'Listener was not running')
        else:
            await self._update_ap_status(ap_id, 'unreachable')
            await self._log_health(ap_id, 'unreachable', f'HTTP and SSH both failed')
            log.warning(f'AP {name} ({ip}): unreachable')

    async def _http_health_check(self, ip):
        """Check AP listener HTTP health endpoint."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f'http://{ip}:{LISTENER_PORT}/health') as resp:
                    if resp.status == 200:
                        return 'healthy'
                    return f'http_{resp.status}'
        except Exception:
            return 'unreachable'

    async def _get_ap_server_ip(self, ip):
        """Get the configured server IP from the AP listener."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f'http://{ip}:{LISTENER_PORT}/status') as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('server_ip')
        except Exception:
            return None

    async def _ssh_check_and_fix(self, ap):
        """SSH into AP, check listener status, deploy if needed."""
        ip = str(ap['ip_address'])
        user = ap['ssh_user']
        password = ap['ssh_password']

        if not self.binary_ready:
            log.error('Cannot deploy: binary not compiled')
            return False

        try:
            async with asyncssh.connect(
                ip, username=user, password=password,
                known_hosts=None, connect_timeout=10
            ) as conn:
                # Check if listener is running
                result = await conn.run('pgrep -f spectral_listener', check=False)
                if result.exit_status == 0:
                    # Running but HTTP failed - might be stuck, kill and restart
                    await conn.run('killall spectral_listener', check=False)
                    await asyncio.sleep(1)

                # Deploy binary via base64
                await self._deploy_via_base64(conn, ip)
                return True

        except Exception as e:
            log.error(f'SSH to {ip} failed: {e}')
            await self._log_health(ap['id'], 'ssh_failed', str(e))
            return False

    async def _deploy_via_base64(self, conn, ap_ip):
        """Deploy the listener binary to AP via base64 encoding."""
        import base64

        with open(LISTENER_BINARY, 'rb') as f:
            binary_data = f.read()

        b64_data = base64.b64encode(binary_data).decode()

        # Write base64, decode, make executable
        result = await conn.run(
            f'echo "{b64_data}" | base64 -d > /tmp/spectral_listener && '
            f'chmod +x /tmp/spectral_listener',
            check=False
        )

        if result.exit_status != 0:
            log.error(f'Deploy to {ap_ip} failed: {result.stderr}')
            return

        # Start the listener (stream mode with health API)
        collector_ip = self.server_ip
        await conn.run(
            f'nohup /tmp/spectral_listener stream wifi0 17 {collector_ip} {COLLECTOR_PORT} '
            f'> /dev/null 2>&1 &',
            check=False
        )

        log.info(f'Deployed and started listener on {ap_ip} -> {collector_ip}:{COLLECTOR_PORT}')

    async def _update_ap_status(self, ap_id, status):
        await self.db.execute(
            'UPDATE access_points SET listener_status = $1 WHERE id = $2',
            status, ap_id
        )

    async def _log_health(self, ap_id, status, details=''):
        await self.db.execute(
            'INSERT INTO ap_health_log (ap_id, status, details) VALUES ($1, $2, $3)',
            ap_id, status, details
        )


async def main():
    manager = APManager()
    await manager.start()


if __name__ == '__main__':
    asyncio.run(main())
