/* Spectral Office Map View */

const POLL_INTERVAL = 5000;
let heatmapCanvas, heatmapCtx;
let mapWrapper;
let editMode = false;

// --- Init ---

document.addEventListener('DOMContentLoaded', () => {
    mapWrapper = document.getElementById('map-wrapper');
    heatmapCanvas = document.getElementById('heatmap');

    initHeatmap();
    renderAPMarkers();
    setupAddAPForm();
    setupEditControls();
    loadSavedLayout();
    loadSensitivity();
    setupOfficeSettingsForm();
    setupEditAPForm();
    poll();
    setInterval(poll, POLL_INTERVAL);
});

// --- Heatmap ---

function initHeatmap() {
    const rect = mapWrapper.getBoundingClientRect();
    heatmapCanvas.width = rect.width;
    heatmapCanvas.height = rect.height;
    heatmapCtx = heatmapCanvas.getContext('2d');

    // Resize observer
    new ResizeObserver(() => {
        const r = mapWrapper.getBoundingClientRect();
        heatmapCanvas.width = r.width;
        heatmapCanvas.height = r.height;
    }).observe(mapWrapper);
}

function drawHeatmap(apData) {
    const w = heatmapCanvas.width;
    const h = heatmapCanvas.height;
    heatmapCtx.clearRect(0, 0, w, h);

    if (!apData || apData.length === 0) return;

    // Draw radial gradient for each AP based on intensity
    apData.forEach(ap => {
        const x = (ap.map_x || 0.5) * w;
        const y = (ap.map_y || 0.5) * h;
        const intensity = ap.intensity || 0;

        if (intensity < 0.05) return;

        const radius = Math.max(80, w * 0.15);
        const gradient = heatmapCtx.createRadialGradient(x, y, 0, x, y, radius);

        // Green (low) -> Yellow (medium) -> Red (high)
        const alpha = Math.min(0.6, intensity * 0.8);
        if (intensity < 0.3) {
            gradient.addColorStop(0, `rgba(34, 197, 94, ${alpha})`);
            gradient.addColorStop(1, 'rgba(34, 197, 94, 0)');
        } else if (intensity < 0.6) {
            gradient.addColorStop(0, `rgba(234, 179, 8, ${alpha})`);
            gradient.addColorStop(1, 'rgba(234, 179, 8, 0)');
        } else {
            gradient.addColorStop(0, `rgba(239, 68, 68, ${alpha})`);
            gradient.addColorStop(1, 'rgba(239, 68, 68, 0)');
        }

        heatmapCtx.fillStyle = gradient;
        heatmapCtx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
    });
}

// --- AP Markers ---

function renderAPMarkers() {
    const container = document.getElementById('ap-markers');
    container.innerHTML = '';

    APS.forEach(ap => {
        const marker = document.createElement('div');
        marker.className = `ap-marker ${ap.listener_status || 'unknown'}`;
        marker.dataset.apId = ap.id;
        marker.style.left = `${(ap.map_x || 0.5) * 100}%`;
        marker.style.top = `${(ap.map_y || 0.5) * 100}%`;
        marker.textContent = 'AP';

        const label = document.createElement('div');
        label.className = 'ap-marker-label';
        label.textContent = ap.name;
        marker.appendChild(label);

        // Drag to reposition
        makeDraggable(marker, ap);

        container.appendChild(marker);
    });
}

function makeDraggable(marker, ap) {
    let dragging = false;
    let startX, startY;

    marker.addEventListener('mousedown', (e) => {
        dragging = true;
        startX = e.clientX;
        startY = e.clientY;
        marker.style.cursor = 'grabbing';
        e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const rect = mapWrapper.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
        marker.style.left = `${x * 100}%`;
        marker.style.top = `${y * 100}%`;
    });

    document.addEventListener('mouseup', (e) => {
        if (!dragging) return;
        dragging = false;
        marker.style.cursor = 'grab';

        const rect = mapWrapper.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));

        // Save new position
        fetch(`/api/aps/${ap.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ map_x: x, map_y: y })
        });
    });
}

// --- Polling ---

async function poll() {
    try {
        const resp = await fetch(`/api/occupancy/office/${OFFICE_ID}`);
        const data = await resp.json();

        const apData = data.aps || [];
        const collector = data.collector || {};

        drawHeatmap(apData);
        updateAPList(apData);
        updateOfficeStatus(apData);
        updateHeartbeat(collector, apData);
    } catch (e) {
        console.error('Poll error:', e);
        updateHeartbeat({ status: 'unreachable' }, []);
    }
}

function updateAPList(apData) {
    apData.forEach(ap => {
        const el = document.getElementById(`ap-intensity-${ap.id}`);
        if (el) {
            const intensity = ap.intensity || 0;
            const pct = Math.round(intensity * 100);
            const lastAgo = ap.last_seen_seconds_ago;

            // Per-AP heartbeat
            let dotClass = 'dead';
            let agoText = 'No data';
            if (lastAgo !== null && lastAgo < 10) {
                dotClass = 'live';
                agoText = `${lastAgo}s ago`;
            } else if (lastAgo !== null && lastAgo < 60) {
                dotClass = 'stale';
                agoText = `${lastAgo}s ago`;
            } else if (lastAgo !== null) {
                dotClass = 'dead';
                const m = Math.floor(lastAgo / 60);
                agoText = `${m}m ago`;
            }

            const color = intensity > 0.5 ? '#ef4444' : intensity > 0.2 ? '#eab308' : '#22c55e';
            el.innerHTML = `<span class="ap-heartbeat ${dotClass}"></span>` +
                `<span style="color:${color}">${pct}%</span> ` +
                `<span style="font-size:0.7rem; color:var(--text-dim);">${agoText}</span>`;
        }

        // Update marker class
        const marker = document.querySelector(`.ap-marker[data-ap-id="${ap.id}"]`);
        if (marker) {
            marker.className = `ap-marker ${ap.receiving ? 'active' : (ap.listener_status || 'unknown')}`;
        }
    });
}

function updateHeartbeat(collector, apData) {
    const banner = document.getElementById('connection-banner');
    const bannerText = document.getElementById('banner-text');

    const lastAgo = collector.last_sample_seconds_ago;
    const status = collector.status;

    // Connection banner - only shows when something is wrong
    if (status === 'unreachable') {
        banner.style.display = 'block';
        bannerText.textContent = 'Collector service unreachable';
    } else if (lastAgo !== null && lastAgo > 30) {
        banner.style.display = 'block';
        const mins = Math.floor(lastAgo / 60);
        const secs = lastAgo % 60;
        bannerText.textContent = mins > 0
            ? `Data feed lost - last received ${mins}m ${secs}s ago`
            : `Data feed lost - last received ${secs}s ago`;
    } else if (status === 'no_data') {
        banner.style.display = 'block';
        bannerText.textContent = 'No data from any AP';
    } else {
        banner.style.display = 'none';
    }
}

function updateOfficeStatus(apData) {
    const el = document.getElementById('office-status');
    const anyOccupied = apData.some(ap => (ap.intensity || 0) > 0.15);

    if (apData.length === 0) {
        el.textContent = 'No APs';
        el.style.borderColor = 'var(--border)';
    } else if (anyOccupied) {
        el.textContent = 'Occupied';
        el.style.borderColor = 'var(--green)';
        el.style.color = 'var(--green)';
    } else {
        el.textContent = 'Empty';
        el.style.borderColor = 'var(--text-dim)';
        el.style.color = 'var(--text-dim)';
    }
}

// --- AP List Toggle ---

function toggleAPList() {
    const body = document.getElementById('ap-table-body');
    const toggle = document.getElementById('ap-toggle');
    if (body.style.display === 'none') {
        body.style.display = '';
        toggle.innerHTML = '&#9660;';
    } else {
        body.style.display = 'none';
        toggle.innerHTML = '&#9654;';
    }
}

// --- Baseline & Sensitivity ---

async function baselineAP(apId) {
    const btn = document.getElementById(`ap-baseline-${apId}`);
    btn.disabled = true;
    btn.textContent = 'Capturing...';

    try {
        await fetch(`/api/aps/${apId}/baseline`, { method: 'POST' });

        // Poll until done
        const poll = setInterval(async () => {
            const resp = await fetch(`/api/aps/${apId}/baseline`);
            const data = await resp.json();
            if (data.status === 'locked') {
                btn.textContent = 'Baseline';
                btn.disabled = false;
                clearInterval(poll);
            } else {
                btn.textContent = `${data.samples}/${data.target}`;
            }
        }, 1000);
    } catch (e) {
        btn.textContent = 'Failed';
        btn.disabled = false;
    }
}

function showBaselineModal() { document.getElementById('baseline-modal').style.display = 'flex'; }
function hideBaselineModal() { document.getElementById('baseline-modal').style.display = 'none'; }

async function startBaseline() {
    const when = document.getElementById('baseline-when').value;
    const duration = parseInt(document.getElementById('baseline-duration').value);
    hideBaselineModal();

    if (when === 'now') {
        runBaselineAll(duration);
    } else {
        const delayMin = parseInt(when);
        const btn = document.querySelector('[onclick="showBaselineModal()"]');
        btn.textContent = `Baseline in ${delayMin}m...`;
        btn.disabled = true;

        // Countdown
        let remaining = delayMin * 60;
        const countdown = setInterval(() => {
            remaining--;
            const m = Math.floor(remaining / 60);
            const s = remaining % 60;
            btn.textContent = `Baseline in ${m}:${s.toString().padStart(2, '0')}`;
            if (remaining <= 0) {
                clearInterval(countdown);
                runBaselineAll(duration);
            }
        }, 1000);
    }
}

async function runBaselineAll(duration) {
    const btn = document.querySelector('[onclick="showBaselineModal()"]');
    btn.textContent = 'Capturing...';
    btn.disabled = true;

    const apBtns = document.querySelectorAll('[id^="ap-baseline-"]');
    for (const apBtn of apBtns) {
        const apId = apBtn.id.replace('ap-baseline-', '');
        apBtn.disabled = true;
        apBtn.textContent = '0/' + (duration * 2);

        fetch(`/api/aps/${apId}/baseline`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ duration: duration })
        });
    }

    // Poll until all done
    const poll = setInterval(async () => {
        let allDone = true;
        for (const apBtn of apBtns) {
            const apId = apBtn.id.replace('ap-baseline-', '');
            const resp = await fetch(`/api/aps/${apId}/baseline`);
            const data = await resp.json();
            if (data.status === 'capturing') {
                apBtn.textContent = `${data.samples}/${data.target}`;
                allDone = false;
            } else if (data.status === 'locked') {
                apBtn.textContent = 'Baseline';
                apBtn.disabled = false;
            }
        }
        if (allDone) {
            clearInterval(poll);
            btn.textContent = 'Baseline All';
            btn.disabled = false;
        }
    }, 2000);
}

async function setSensitivity(value) {
    document.getElementById('sensitivity-val').textContent = value.toFixed(1) + 'x';
    await fetch(`/api/offices/${OFFICE_ID}/sensitivity`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sensitivity: value })
    });
}

// Load current sensitivity on init
async function loadSensitivity() {
    try {
        const resp = await fetch(`/api/offices/${OFFICE_ID}/sensitivity`);
        const data = await resp.json();
        const val = data.sensitivity || 1.0;
        document.getElementById('sensitivity-slider').value = val * 10;
        document.getElementById('sensitivity-val').textContent = val.toFixed(1) + 'x';
    } catch (e) {}
}

// --- AP Check & Deploy ---

async function checkAP(apId) {
    const btn = document.getElementById(`ap-check-${apId}`);
    const statusEl = document.getElementById(`ap-lstatus-${apId}`);
    btn.disabled = true;
    btn.textContent = 'Checking...';
    statusEl.innerHTML = '<span class="lstatus-unknown">Checking...</span>';

    try {
        const resp = await fetch(`/api/aps/${apId}/check`, { method: 'POST' });
        const data = await resp.json();

        const dot = document.getElementById(`ap-dot-${apId}`);
        const deployBtn = document.getElementById(`ap-deploy-${apId}`);
        const intensityEl = document.getElementById(`ap-intensity-${apId}`);

        // Collector flow status
        const flow = data.collector || {};
        if (flow.receiving) {
            intensityEl.innerHTML = `<span style="color:var(--green)">${flow.samples} samples</span>`;
        } else {
            intensityEl.innerHTML = `<span style="color:var(--red)">No data</span>`;
        }

        if (data.status === 'installed') {
            statusEl.innerHTML = `<span class="lstatus-ok">Running</span><br><span style="font-size:0.7rem;color:var(--text-dim)">${data.details.samples_sent} sent, up ${Math.round(data.details.uptime_seconds/60)}m</span>`;
            dot.className = 'ap-status-dot deployed';
            deployBtn.textContent = 'Update';
        } else if (data.status === 'stopped') {
            statusEl.innerHTML = '<span class="lstatus-warn">Stopped</span><br><span style="font-size:0.7rem;color:var(--text-dim)">Binary exists but not running</span>';
            dot.className = 'ap-status-dot stopped';
            deployBtn.textContent = 'Start';
        } else if (data.status === 'not_installed') {
            statusEl.innerHTML = '<span class="lstatus-none">Not Installed</span>';
            dot.className = 'ap-status-dot not_installed';
            deployBtn.textContent = 'Install';
        } else {
            statusEl.innerHTML = `<span class="lstatus-unknown">Unreachable</span><br><span style="font-size:0.7rem;color:var(--text-dim)">${data.details}</span>`;
            dot.className = 'ap-status-dot unreachable';
        }
    } catch (e) {
        statusEl.innerHTML = `<span class="lstatus-unknown">Error: ${e.message}</span>`;
    }

    btn.disabled = false;
    btn.textContent = 'Check';
}

async function deployAP(apId) {
    const btn = document.getElementById(`ap-deploy-${apId}`);
    const statusEl = document.getElementById(`ap-lstatus-${apId}`);

    if (!confirm('Deploy spectral listener to this AP?')) return;

    btn.disabled = true;
    btn.textContent = 'Deploying...';
    statusEl.innerHTML = '<span class="lstatus-unknown">Compiling & deploying...</span>';

    try {
        const resp = await fetch(`/api/aps/${apId}/deploy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ server_ip: SERVER_IP })
        });
        const data = await resp.json();

        if (data.status === 'deployed') {
            statusEl.innerHTML = `<span class="lstatus-ok">Deployed!</span><br><span style="font-size:0.7rem;color:var(--text-dim)">Streaming to ${data.server_ip}</span>`;
            document.getElementById(`ap-dot-${apId}`).className = 'ap-status-dot deployed';
            btn.textContent = 'Update';
        } else {
            statusEl.innerHTML = `<span class="lstatus-unknown">Failed: ${data.step}</span><br><span style="font-size:0.7rem;color:var(--red)">${data.details}</span>`;
            btn.textContent = 'Retry';
        }
    } catch (e) {
        statusEl.innerHTML = `<span class="lstatus-unknown">Error: ${e.message}</span>`;
        btn.textContent = 'Retry';
    }

    btn.disabled = false;
}

// --- Add AP ---

// --- Discover APs ---

function showDiscover() {
    document.getElementById('discover-modal').style.display = 'flex';
    const warn = document.getElementById('discover-creds-warning');
    const btn = document.getElementById('discover-scan-btn');
    if (!DEFAULT_SSH_USER || !DEFAULT_SSH_PASSWORD) {
        warn.style.display = 'block';
        btn.disabled = true;
    } else {
        warn.style.display = 'none';
        btn.disabled = false;
    }
}
function hideDiscover() {
    document.getElementById('discover-modal').style.display = 'none';
    // Reload to pick up any newly added APs
    if (document.querySelector('#discover-results .btn[disabled]')) {
        location.reload();
    }
}

async function runDiscover() {
    const subnet = document.getElementById('discover-subnet').value;
    if (!subnet) return;

    const btn = document.getElementById('discover-scan-btn');
    const status = document.getElementById('discover-status');
    const results = document.getElementById('discover-results');

    btn.disabled = true;
    btn.textContent = 'Scanning...';
    status.textContent = 'Scanning subnet, this may take a minute...';
    results.innerHTML = '';

    try {
        const resp = await fetch('/api/discover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                subnet: subnet,
                ssh_user: DEFAULT_SSH_USER,
                ssh_password: DEFAULT_SSH_PASSWORD
            })
        });
        const data = await resp.json();

        if (data.error) {
            status.textContent = `Error: ${data.error}`;
        } else {
            const found = data.found || [];
            const newCount = found.filter(a => !a.already_registered).length;
            status.textContent = `Scanned ${data.scanned} hosts, found ${found.length} APs (${newCount} new)`;

            const addAllBtn = document.getElementById('discover-add-all-btn');
            if (newCount > 0) {
                addAllBtn.style.display = '';
                addAllBtn.textContent = `Add All (${newCount})`;
            }

            results.innerHTML = found.map(ap => `
                <div class="ap-row" style="margin-bottom:0.25rem;">
                    <div class="ap-status-dot ${ap.has_module ? 'deployed' : 'unknown'}"></div>
                    <div class="ap-info" style="flex:1;">
                        <strong>${ap.hostname}</strong>
                        <span class="ap-ip">${ap.ip}</span>
                        ${ap.has_spectral ? '<span style="color:var(--green); font-size:0.75rem;">spectral ready</span>' : ''}
                        ${ap.already_registered ? '<span style="color:var(--text-dim); font-size:0.75rem;">already added</span>' : ''}
                    </div>
                    ${ap.already_registered ? '' : `
                        <button class="btn btn-sm" onclick="addDiscoveredAP('${ap.ip}', '${ap.hostname}', this)">Add</button>
                    `}
                </div>
            `).join('');
        }
    } catch (e) {
        status.textContent = `Error: ${e.message}`;
    }

    btn.disabled = false;
    btn.textContent = 'Scan';
}

async function addAllDiscovered() {
    const btns = document.querySelectorAll('#discover-results .btn.btn-sm:not([disabled])');
    const addAllBtn = document.getElementById('discover-add-all-btn');
    addAllBtn.disabled = true;
    addAllBtn.textContent = 'Adding...';

    for (const btn of btns) {
        btn.click();
        await new Promise(r => setTimeout(r, 200));
    }

    addAllBtn.textContent = 'All Added';
}

async function addDiscoveredAP(ip, hostname, btn) {
    btn.disabled = true;
    btn.textContent = 'Adding...';

    const resp = await fetch('/api/aps', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            office_id: OFFICE_ID,
            name: hostname,
            ip_address: ip,
            ssh_user: DEFAULT_SSH_USER,
            ssh_password: DEFAULT_SSH_PASSWORD,
            model: 'U7 Pro Max'
        })
    });

    if (resp.ok) {
        btn.textContent = 'Added';
        btn.style.color = 'var(--green)';
    } else {
        btn.textContent = 'Failed';
        btn.style.color = 'var(--red)';
        btn.disabled = false;
    }
}

// --- Edit / Delete AP ---

function editAP(apId, name, ip) {
    document.getElementById('edit-ap-id').value = apId;
    document.getElementById('edit-ap-name').value = name;
    document.getElementById('edit-ap-ip').value = ip;
    document.getElementById('edit-ap-user').value = '';
    document.getElementById('edit-ap-pass').value = '';
    document.getElementById('edit-ap-modal').style.display = 'flex';
}

function hideEditAP() { document.getElementById('edit-ap-modal').style.display = 'none'; }

function setupEditAPForm() {
    const form = document.getElementById('edit-ap-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const apId = document.getElementById('edit-ap-id').value;
        const data = { name: form.name.value, ip_address: form.ip_address.value };
        if (form.ssh_user.value) data.ssh_user = form.ssh_user.value;
        if (form.ssh_password.value) data.ssh_password = form.ssh_password.value;

        await fetch(`/api/aps/${apId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        location.reload();
    });
}

async function deleteAP(apId, name) {
    if (!confirm(`Remove ${name}? This will stop monitoring this AP.`)) return;
    await fetch(`/api/aps/${apId}`, { method: 'DELETE' });
    location.reload();
}

// --- Add AP ---

function showAddAP() {
    document.getElementById('ap-ssh-user').value = DEFAULT_SSH_USER;
    document.getElementById('ap-ssh-pass').value = DEFAULT_SSH_PASSWORD;
    document.getElementById('add-ap-modal').style.display = 'flex';
}
function hideAddAP() { document.getElementById('add-ap-modal').style.display = 'none'; }

// --- Office Settings ---

async function showOfficeSettings() {
    document.getElementById('office-settings-modal').style.display = 'flex';
    // Load schedule
    try {
        const resp = await fetch(`/api/offices/${OFFICE_ID}/schedule`);
        const data = await resp.json();
        document.getElementById('sched-time').value = data.time || '02:00';
        document.getElementById('sched-duration').value = data.duration || 300;
        document.getElementById('sched-enabled').checked = data.enabled || false;
        document.getElementById('sched-last-run').textContent = data.last_run
            ? `Last run: ${new Date(data.last_run).toLocaleString()}` : 'Never run';
    } catch (e) {}
}
function hideOfficeSettings() { document.getElementById('office-settings-modal').style.display = 'none'; }

function setupOfficeSettingsForm() {
    const form = document.getElementById('office-settings-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        // Save office settings
        await fetch(`/api/offices/${OFFICE_ID}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: form.name.value,
                location: form.location.value,
                default_ssh_user: form.default_ssh_user.value,
                default_ssh_password: form.default_ssh_password.value,
            })
        });
        // Save schedule
        await fetch(`/api/offices/${OFFICE_ID}/schedule`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                time: document.getElementById('sched-time').value,
                duration: parseInt(document.getElementById('sched-duration').value),
                enabled: document.getElementById('sched-enabled').checked,
            })
        });
        location.reload();
    });
}

function setupAddAPForm() {
    document.getElementById('add-ap-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const form = e.target;
        const data = {
            office_id: OFFICE_ID,
            name: form.name.value,
            ip_address: form.ip_address.value,
            ssh_user: form.ssh_user.value,
            ssh_password: form.ssh_password.value,
            model: form.model.value,
        };

        const resp = await fetch('/api/aps', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        if (resp.ok) {
            location.reload();
        }
    });
}

// --- Floor Plan Upload + Crop ---

function uploadFloorPlan() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        showCropModal(URL.createObjectURL(file));
    };
    input.click();
}

function showCropModal(src) {
    // Remove existing modal if any
    document.getElementById('crop-modal')?.remove();

    const modal = document.createElement('div');
    modal.id = 'crop-modal';
    modal.className = 'modal';
    modal.style.display = 'flex';
    modal.innerHTML = `
        <div class="modal-content" style="max-width:90vw; max-height:90vh; overflow:hidden; padding:1rem;">
            <h3>Crop Floor Plan</h3>
            <p style="font-size:0.8rem; color:var(--text-dim); margin-bottom:0.5rem;">
                Click and drag to select the area to keep.
            </p>
            <div id="crop-container" style="position:relative; display:inline-block; cursor:crosshair; max-height:70vh; overflow:auto;">
                <img id="crop-img" src="${src}" style="display:block; max-width:80vw; max-height:65vh;">
                <canvas id="crop-overlay" style="position:absolute; top:0; left:0; pointer-events:none;"></canvas>
            </div>
            <div class="modal-actions" style="margin-top:0.75rem;">
                <button class="btn btn-secondary" onclick="closeCropModal()">Cancel</button>
                <button class="btn" id="crop-save-btn" disabled>Crop & Save</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    const img = document.getElementById('crop-img');
    const overlay = document.getElementById('crop-overlay');
    const ctx = overlay.getContext('2d');
    const container = document.getElementById('crop-container');

    let crop = { startX: 0, startY: 0, endX: 0, endY: 0, dragging: false };

    img.onload = () => {
        overlay.width = img.clientWidth;
        overlay.height = img.clientHeight;
    };

    // If already loaded
    if (img.complete) {
        overlay.width = img.clientWidth;
        overlay.height = img.clientHeight;
    }

    // Use document-level listeners so mouseup is never missed
    container.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const rect = img.getBoundingClientRect();
        crop.startX = e.clientX - rect.left;
        crop.startY = e.clientY - rect.top;
        crop.endX = crop.startX;
        crop.endY = crop.startY;
        crop.dragging = true;
        document.getElementById('crop-save-btn').disabled = true;
    });

    document.addEventListener('mousemove', (e) => {
        if (!crop.dragging) return;
        const rect = img.getBoundingClientRect();
        crop.endX = Math.max(0, Math.min(img.clientWidth, e.clientX - rect.left));
        crop.endY = Math.max(0, Math.min(img.clientHeight, e.clientY - rect.top));
        drawCropOverlay(ctx, overlay.width, overlay.height, crop);
    });

    document.addEventListener('mouseup', () => {
        if (!crop.dragging) return;
        crop.dragging = false;
        const w = Math.abs(crop.endX - crop.startX);
        const h = Math.abs(crop.endY - crop.startY);
        if (w > 10 && h > 10) {
            document.getElementById('crop-save-btn').disabled = false;
        }
    });

    document.getElementById('crop-save-btn').addEventListener('click', () => {
        saveCroppedFloorPlan(img, crop);
    });
}

function drawCropOverlay(ctx, w, h, crop) {
    ctx.clearRect(0, 0, w, h);

    // Dim everything
    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.fillRect(0, 0, w, h);

    // Clear the crop area
    const x = Math.min(crop.startX, crop.endX);
    const y = Math.min(crop.startY, crop.endY);
    const cw = Math.abs(crop.endX - crop.startX);
    const ch = Math.abs(crop.endY - crop.startY);
    ctx.clearRect(x, y, cw, ch);

    // Border around crop area
    ctx.strokeStyle = '#3b82f6';
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, cw, ch);
}

async function saveCroppedFloorPlan(img, crop) {
    // Map from display coords to natural image coords
    const scaleX = img.naturalWidth / img.clientWidth;
    const scaleY = img.naturalHeight / img.clientHeight;

    const sx = Math.min(crop.startX, crop.endX) * scaleX;
    const sy = Math.min(crop.startY, crop.endY) * scaleY;
    const sw = Math.abs(crop.endX - crop.startX) * scaleX;
    const sh = Math.abs(crop.endY - crop.startY) * scaleY;

    // Limit output size
    const MAX_W = 1200;
    const MAX_H = 900;
    let outW = sw;
    let outH = sh;
    if (outW > MAX_W) { outH = outH * (MAX_W / outW); outW = MAX_W; }
    if (outH > MAX_H) { outW = outW * (MAX_H / outH); outH = MAX_H; }

    const canvas = document.createElement('canvas');
    canvas.width = outW;
    canvas.height = outH;
    canvas.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, outW, outH);

    const dataUrl = canvas.toDataURL('image/jpeg', 0.6);

    closeCropModal();
    showCleanupModal(dataUrl);
}

function showCleanupModal(dataUrl) {
    document.getElementById('cleanup-modal')?.remove();

    const modal = document.createElement('div');
    modal.id = 'cleanup-modal';
    modal.className = 'modal';
    modal.style.display = 'flex';
    modal.innerHTML = `
        <div class="modal-content" style="max-width:90vw; padding:1rem;">
            <h3>Clean Up Blueprint?</h3>
            <p style="font-size:0.8rem; color:var(--text-dim); margin-bottom:0.75rem;">
                If this is a photo of a blueprint, cleanup will sharpen lines and remove noise. All processing is local - nothing leaves this server.
            </p>
            <div style="display:flex; gap:1rem; margin-bottom:0.75rem; flex-wrap:wrap;">
                <div style="text-align:center;">
                    <div style="font-size:0.75rem; color:var(--text-dim); margin-bottom:0.25rem;">Original</div>
                    <img id="cleanup-original" src="${dataUrl}" style="max-width:35vw; max-height:40vh; border:1px solid var(--border); border-radius:6px;">
                </div>
                <div style="text-align:center;">
                    <div style="font-size:0.75rem; color:var(--text-dim); margin-bottom:0.25rem;">Preview</div>
                    <img id="cleanup-preview" src="${dataUrl}" style="max-width:35vw; max-height:40vh; border:1px solid var(--border); border-radius:6px;">
                </div>
            </div>
            <div style="display:flex; gap:0.5rem; margin-bottom:0.75rem;">
                <button class="btn btn-sm btn-secondary" onclick="previewCleanup('blueprint')">Blueprint</button>
                <button class="btn btn-sm btn-secondary" onclick="previewCleanup('high_contrast')">High Contrast</button>
                <button class="btn btn-sm btn-secondary" onclick="previewCleanup('photo')">Light Touch</button>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="saveFloorPlanDirect()">Skip - Use Original</button>
                <button class="btn" id="cleanup-save-btn" onclick="saveCleanedFloorPlan()" disabled>Use Cleaned</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // Store original for cleanup
    window._cleanupOriginal = dataUrl;
    window._cleanupResult = null;
}

async function previewCleanup(mode) {
    const preview = document.getElementById('cleanup-preview');
    const saveBtn = document.getElementById('cleanup-save-btn');
    preview.style.opacity = '0.3';
    saveBtn.disabled = true;
    saveBtn.textContent = 'Processing...';

    try {
        const resp = await fetch('/api/cleanup-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image: window._cleanupOriginal, mode: mode })
        });
        const data = await resp.json();
        if (data.image) {
            preview.src = data.image;
            window._cleanupResult = data.image;
            saveBtn.disabled = false;
            saveBtn.textContent = 'Use Cleaned';
        }
    } catch (e) {
        saveBtn.textContent = 'Failed';
    }
    preview.style.opacity = '1';
}

async function saveCleanedFloorPlan() {
    if (!window._cleanupResult) return;
    await fetch(`/api/offices/${OFFICE_ID}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ floor_plan_url: window._cleanupResult })
    });
    document.getElementById('cleanup-modal')?.remove();
    location.reload();
}

async function saveFloorPlanDirect() {
    await fetch(`/api/offices/${OFFICE_ID}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ floor_plan_url: window._cleanupOriginal })
    });
    document.getElementById('cleanup-modal')?.remove();
    location.reload();
}

function closeCropModal() {
    document.getElementById('crop-modal')?.remove();
}

// --- Floor Plan Edit Mode ---
// "Edit Layout" re-opens the crop tool on the current floor plan image
// so the user can re-crop to remove unwanted borders.

function setupEditControls() {
    // Nothing to set up - edit just re-opens crop modal
}

function loadSavedLayout() {
    // No transform-based layout - cropping is baked into the image
}

function toggleEditMode() {
    const fp = document.getElementById('floor-plan');
    if (!fp || fp.tagName !== 'IMG') return;
    showCropModal(fp.src);
}
