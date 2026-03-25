/* Spectral Global Office View */

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
        const badgeEl = document.getElementById(`badge-${office.id}`);
        const detailsEl = document.getElementById(`details-${office.id}`);

        if (badgeEl) {
            const apCount = office.ap_count || 0;
            const activeCount = office.active_ap_count || 0;

            if (apCount === 0 || activeCount === 0) {
                badgeEl.textContent = 'No Data';
                badgeEl.className = 'office-card-badge unknown';
            } else if (office.occupied) {
                badgeEl.textContent = 'Occupied';
                badgeEl.className = 'office-card-badge occupied';
            } else {
                badgeEl.textContent = 'Vacant';
                badgeEl.className = 'office-card-badge vacant';
            }
        }

        if (detailsEl) {
            const apCount = office.ap_count || 0;
            const activeCount = office.active_ap_count || 0;
            const intensity = office.avg_intensity || 0;

            let statusText = '';
            if (apCount === 0) {
                statusText = 'No APs configured';
            } else {
                statusText = `${activeCount}/${apCount} APs reporting`;
                if (activeCount > 0) {
                    statusText += ` - ${Math.round(intensity * 100)}% activity`;
                }
            }

            detailsEl.querySelector('.ap-count').textContent = statusText;
        }
    });
}

// --- Manage Offices ---

function showManage() { document.getElementById('manage-modal').style.display = 'flex'; }
function hideManage() { document.getElementById('manage-modal').style.display = 'none'; }

async function deleteOffice(id, name) {
    if (!confirm(`Remove "${name}"? This will delete all APs and data for this office.`)) return;

    const resp = await fetch(`/api/offices/${id}`, { method: 'DELETE' });
    if (resp.ok) {
        const row = document.getElementById(`manage-office-${id}`);
        if (row) {
            row.style.opacity = '0.3';
            row.querySelector('button').disabled = true;
            row.querySelector('button').textContent = 'Removed';
        }
        // Remove the card from the grid
        const card = document.querySelector(`.office-card[data-office-id="${id}"]`);
        if (card) card.remove();
    }
}

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
