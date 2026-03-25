/* C-You Office Map View */

const POLL_INTERVAL = 5000;
let heatmapCanvas, heatmapCtx;
let mapWrapper;

// --- Init ---

document.addEventListener('DOMContentLoaded', () => {
    mapWrapper = document.getElementById('map-wrapper');
    heatmapCanvas = document.getElementById('heatmap');

    initHeatmap();
    renderAPMarkers();
    setupAddAPForm();
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

        drawHeatmap(data);
        updateAPList(data);
        updateOfficeStatus(data);
    } catch (e) {
        console.error('Poll error:', e);
    }
}

function updateAPList(apData) {
    apData.forEach(ap => {
        const el = document.getElementById(`ap-intensity-${ap.id}`);
        if (el) {
            const intensity = ap.intensity || 0;
            const pct = Math.round(intensity * 100);
            el.textContent = `${pct}%`;
            el.style.color = intensity > 0.5 ? '#ef4444' :
                             intensity > 0.2 ? '#eab308' : '#22c55e';
        }

        // Update marker class
        const marker = document.querySelector(`.ap-marker[data-ap-id="${ap.id}"]`);
        if (marker) {
            marker.className = `ap-marker ${ap.listener_status === 'deployed' ? 'active' : (ap.listener_status || 'unknown')}`;
        }
    });
}

function updateOfficeStatus(apData) {
    const el = document.getElementById('office-status');
    const anyOccupied = apData.some(ap => (ap.intensity || 0) > 0.15);
    const allReporting = apData.every(ap => ap.listener_status === 'deployed');

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

// --- Add AP ---

function showAddAP() { document.getElementById('add-ap-modal').style.display = 'flex'; }
function hideAddAP() { document.getElementById('add-ap-modal').style.display = 'none'; }

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

// --- Floor Plan Upload ---

function uploadFloorPlan() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        // For now, use data URL (in production, upload to storage)
        const reader = new FileReader();
        reader.onload = async () => {
            await fetch(`/api/offices/${OFFICE_ID}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ floor_plan_url: reader.result })
            });
            location.reload();
        };
        reader.readAsDataURL(file);
    };
    input.click();
}
