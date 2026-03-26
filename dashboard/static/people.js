/* Spectral - Who's Here */

const POLL_INTERVAL = 10000;
let searchTimeout;

document.addEventListener('DOMContentLoaded', () => {
    poll();
    setInterval(poll, POLL_INTERVAL);
    loadFirstIn();
    setInterval(loadFirstIn, 60000);

    document.getElementById('search-input').addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => doSearch(e.target.value), 300);
    });
});

async function poll() {
    try {
        const resp = await fetch(`/api/clients/${OFFICE_ID}`);
        const data = await resp.json();
        renderClients(data.clients || []);
        renderStatic(data.static || []);
        document.getElementById('people-count').textContent = `${data.count || 0} people, ${data.static_count || 0} static`;
        document.getElementById('connected-count').textContent = `(${data.count || 0})`;
        document.getElementById('static-count').textContent = `(${data.static_count || 0})`;
    } catch (e) {
        console.error('Poll error:', e);
    }
}

function formatUptime(seconds) {
    if (!seconds) return '';
    if (seconds < 3600) return `${Math.floor(seconds/60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds/3600)}h`;
    return `${Math.floor(seconds/86400)}d`;
}

function formatTime(isoString) {
    if (!isoString) return '--';
    return new Date(isoString).toLocaleTimeString('en-US', {
        timeZone: OFFICE_TIMEZONE,
        hour: 'numeric',
        minute: '2-digit',
        hour12: true
    });
}

function formatDateTime(isoString) {
    if (!isoString) return '--';
    return new Date(isoString).toLocaleString('en-US', {
        timeZone: OFFICE_TIMEZONE,
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true
    });
}

function renderClients(clients) {
    const list = document.getElementById('client-list');

    if (clients.length === 0) {
        list.innerHTML = '<p style="color:var(--text-dim); font-size:0.85rem;">No active clients</p>';
        return;
    }

    list.innerHTML = clients.map(c => `
        <div class="ap-row" style="margin-bottom:0.25rem;">
            <div class="ap-status-dot deployed"></div>
            <div class="ap-info" style="flex:1;">
                <strong>${c.name}</strong>
                ${c.identity ? `<span style="font-size:0.75rem; color:var(--green); margin-left:0.25rem;">${c.identity}</span>` : ''}
                <br>
                <span class="ap-ip">${c.hostname || ''}</span>
                <span class="ap-ip">${c.ip || ''}</span>
                <span class="ap-ip">${c.ssid} (${c.radio})</span>
                <span class="ap-ip">up ${formatUptime(c.uptime)}</span>
            </div>
            <div style="text-align:right; font-size:0.8rem;">
                <div style="color:var(--text-dim);">${c.ap_name}</div>
                <div>RSSI: ${c.rssi || '--'}</div>
            </div>
            <button class="btn btn-sm btn-secondary" style="margin-left:0.5rem; font-size:0.7rem;"
                    onclick="markStatic('${c.mac}', '${c.name}')">Static</button>
        </div>
    `).join('');
}

function renderStatic(devices) {
    const list = document.getElementById('static-list');

    if (devices.length === 0) {
        list.innerHTML = '<p style="color:var(--text-dim); font-size:0.85rem;">No static devices</p>';
        return;
    }

    list.innerHTML = devices.map(c => `
        <div class="ap-row" style="margin-bottom:0.25rem; opacity:0.6;">
            <div class="ap-status-dot" style="background:var(--border);"></div>
            <div class="ap-info" style="flex:1;">
                <strong>${c.name}</strong>
                ${c.static_label ? `<span style="font-size:0.75rem; color:var(--text-dim); margin-left:0.25rem;">${c.static_label}</span>` : ''}
                <br>
                <span class="ap-ip">${c.hostname || ''}</span>
                <span class="ap-ip">${c.ip || ''}</span>
                <span class="ap-ip">up ${formatUptime(c.uptime)}</span>
            </div>
            <div style="text-align:right; font-size:0.8rem; color:var(--text-dim);">
                ${c.ap_name}
            </div>
            <button class="btn btn-sm btn-secondary" style="margin-left:0.5rem; font-size:0.7rem;"
                    onclick="unmarkStatic('${c.mac}')">Unmark</button>
        </div>
    `).join('');
}

async function markStatic(mac, name) {
    const label = prompt(`Label for this static device (e.g. "Conference Room TV", "Lobby iPad"):`, name);
    if (label === null) return;

    await fetch('/api/clients/static', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: mac, is_static: true, label: label })
    });
    poll();
}

async function unmarkStatic(mac) {
    await fetch('/api/clients/static', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: mac, is_static: false, label: '' })
    });
    poll();
}

async function loadFirstIn() {
    try {
        const resp = await fetch(`/api/clients/${OFFICE_ID}/first-in`);
        const data = await resp.json();
        renderFirstIn(data.people || []);
    } catch (e) {}
}

function renderFirstIn(people) {
    const list = document.getElementById('first-in-list');

    if (people.length === 0) {
        list.innerHTML = '<p style="color:var(--text-dim); font-size:0.85rem;">No arrivals today</p>';
        return;
    }

    list.innerHTML = people.map((p, i) => {
        const time = formatTime(p.first_seen);
        const badge = i === 0 ? '<span style="color:var(--yellow); font-size:0.75rem; margin-left:0.5rem;">FIRST IN</span>' : '';
        const statusDot = p.here_now ? 'deployed' : 'unknown';
        const statusText = p.here_now ? 'Here now' : 'Left';

        return `
            <div class="ap-row" style="margin-bottom:0.25rem;">
                <div class="ap-status-dot ${statusDot}"></div>
                <div class="ap-info" style="flex:1;">
                    <strong>${p.name}</strong>${badge}
                    <br>
                    <span class="ap-ip">Arrived ${time}</span>
                    <span class="ap-ip">${p.ap_name}</span>
                </div>
                <div style="font-size:0.8rem; color:var(--text-dim);">${statusText}</div>
            </div>
        `;
    }).join('');
}

async function doSearch(query) {
    const results = document.getElementById('search-results');

    if (!query || query.length < 2) {
        results.innerHTML = '';
        return;
    }

    try {
        const resp = await fetch(`/api/clients/${OFFICE_ID}/search?q=${encodeURIComponent(query)}`);
        const data = await resp.json();

        if (data.results.length === 0) {
            results.innerHTML = '<p style="color:var(--text-dim); font-size:0.85rem;">No matches</p>';
            return;
        }

        results.innerHTML = data.results.map(r => {
            const statusDot = r.here_now ? 'deployed' : 'unknown';
            const statusText = r.here_now ? 'Here now' : 'Last seen ' + formatDateTime(r.last_seen);

            return `
                <div class="ap-row" style="margin-bottom:0.25rem;">
                    <div class="ap-status-dot ${statusDot}"></div>
                    <div class="ap-info" style="flex:1;">
                        <strong>${r.name}</strong>
                        <br>
                        <span class="ap-ip">${r.hostname || ''} ${r.ip || ''}</span>
                        <span class="ap-ip">${r.ap_name}</span>
                    </div>
                    <div style="font-size:0.8rem; color:var(--text-dim);">${statusText}</div>
                </div>
            `;
        }).join('');
    } catch (e) {
        results.innerHTML = '<p style="color:var(--red); font-size:0.85rem;">Search error</p>';
    }
}
