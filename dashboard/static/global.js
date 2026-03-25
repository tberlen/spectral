/* C-You Global Office View */

const POLL_INTERVAL = 10000;

document.addEventListener('DOMContentLoaded', () => {
    setupAddOfficeForm();
    poll();
    setInterval(poll, POLL_INTERVAL);
});

async function poll() {
    try {
        const resp = await fetch('/api/occupancy/global');
        const data = await resp.json();
        updateCards(data);
    } catch (e) {
        console.error('Poll error:', e);
    }
}

function updateCards(offices) {
    offices.forEach(office => {
        const statusEl = document.getElementById(`status-${office.id}`);
        const detailsEl = document.getElementById(`details-${office.id}`);

        if (statusEl) {
            const indicator = statusEl.querySelector('.status-indicator');
            const occupied = office.occupied;
            const intensity = office.avg_intensity || 0;

            if (office.ap_count === 0 || office.last_update === null) {
                indicator.className = 'status-indicator unknown';
            } else if (occupied) {
                indicator.className = intensity > 0.5 ?
                    'status-indicator occupied' : 'status-indicator partial';
            } else {
                indicator.className = 'status-indicator empty';
            }
        }

        if (detailsEl) {
            const apCount = office.ap_count || 0;
            const activeCount = office.active_ap_count || 0;
            const intensity = office.avg_intensity || 0;

            let statusText = '';
            if (apCount === 0) {
                statusText = 'No APs configured';
            } else if (office.occupied) {
                statusText = `Occupied (${Math.round(intensity * 100)}%) - ${activeCount}/${apCount} APs`;
            } else {
                statusText = `Empty - ${activeCount}/${apCount} APs`;
            }

            detailsEl.querySelector('.ap-count').textContent = statusText;
        }
    });
}

// --- Add Office ---

function showAddOffice() { document.getElementById('add-office-modal').style.display = 'flex'; }
function hideAddOffice() { document.getElementById('add-office-modal').style.display = 'none'; }

function setupAddOfficeForm() {
    const form = document.getElementById('add-office-form');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const resp = await fetch('/api/offices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: form.name.value,
                location: form.location.value,
            })
        });

        if (resp.ok) {
            location.reload();
        }
    });
}
