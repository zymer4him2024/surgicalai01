import { getFirestore, collection, query, orderBy, limit, onSnapshot, doc, where } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js";
import { app } from "./firebase-config.js";
import { setupAuthUI, getCurrentUser } from "./auth.js";

const db = getFirestore(app);

// Escapes user-controlled strings before inserting into innerHTML templates.
// Prevents XSS from malicious Firestore document values.
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// DOM Elements
const authOverlay = document.getElementById('auth-overlay');
const pendingOverlay = document.getElementById('pending-overlay');
const mainContent = document.getElementById('main-content');
const metricSuccess = document.getElementById('metric-success');
const metricTotal = document.getElementById('metric-total');
const userEmailDisplay = document.getElementById('user-email');

// New DOM Elements
const timelineFeed = document.getElementById('timeline-feed');
const missingWidget = document.getElementById('missing-widget');

let throughputChart = null;
let unsubscribeSnapshot = null;
let unsubscribeInspectionLog = null;

// Projects tab data
let companyProjectsData = [];
let companyCustomersData = [];
let companyDevicesData = [];
let companyModelsData = [];
let companyApplicationsData = [];
let unsubscribeProjects = null;
let unsubscribeCustomers = null;
let unsubscribeDevices = null;
let unsubscribeModels = null;
let unsubscribeApplications = null;

// Auth Callbacks
function _cleanupListeners() {
    if (unsubscribeSnapshot) { unsubscribeSnapshot(); unsubscribeSnapshot = null; }
    if (unsubscribeInspectionLog) { unsubscribeInspectionLog(); unsubscribeInspectionLog = null; }
    if (unsubscribeProjects) { unsubscribeProjects(); unsubscribeProjects = null; }
    if (unsubscribeCustomers) { unsubscribeCustomers(); unsubscribeCustomers = null; }
    if (unsubscribeDevices) { unsubscribeDevices(); unsubscribeDevices = null; }
    if (unsubscribeModels) { unsubscribeModels(); unsubscribeModels = null; }
    if (unsubscribeApplications) { unsubscribeApplications(); unsubscribeApplications = null; }
}

function requireLogin() {
    authOverlay.classList.remove('hidden');
    pendingOverlay.style.display = 'none';
    mainContent.classList.add('hidden');
    userEmailDisplay.innerText = '';
    _cleanupListeners();
}

function pendingApproval(user) {
    authOverlay.classList.add('hidden');
    mainContent.classList.add('hidden');
    pendingOverlay.style.display = 'flex';
    const el = document.getElementById('pending-email');
    if (el) el.textContent = user.email;
    _cleanupListeners();
}

function loggedIn(user) {
    authOverlay.classList.add('hidden');
    pendingOverlay.style.display = 'none';
    mainContent.classList.remove('hidden');
    userEmailDisplay.innerText = user.email;
    initListener();
    initInspectionLogListener();
    initProjectsListeners();
}

setupAuthUI(requireLogin, loggedIn, pendingApproval);

// Data Processing
function processData(eventsData) {
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();

    // Filter today's events for metrics
    const todaysEvents = eventsData.filter(e => e.timestamp && e.timestamp.toMillis() >= startOfToday);

    const totalToday = todaysEvents.length;
    const errorsToday = todaysEvents.filter(e => e.event_type === 'mismatch' || e.event_type === 'alert').length;
    const successesToday = totalToday - errorsToday;
    const successRate = totalToday > 0 ? Math.round((successesToday / totalToday) * 100) : 0;

    metricTotal.innerText = totalToday;
    metricSuccess.innerText = `${successRate}%`;

    // Calculate hourly throughput for the chart
    const hourlyCounts = Array(24).fill(0);
    const missingCounts = {}; // Track missing items

    todaysEvents.forEach(e => {
        const hour = new Date(e.timestamp.toMillis()).getHours();
        hourlyCounts[hour]++;

        // Analyze missing items
        if (e.event_type === 'mismatch' || e.event_type === 'alert') {
            const expectedTokens = e.metadata?.target || {}; // Might need schema check depending on backend
            // Fallback to iterating missing_items array if backend provided it
            const missingList = e.missing_items || [];
            missingList.forEach(item => {
                missingCounts[item.class_name] = (missingCounts[item.class_name] || 0) + item.count;
            });
            // If the backend didn't provide `missing_items`, use devices_resolved or raw data to diff (optional logic based on your backend shape)
        }
    });

    // Prepare labels
    const currentHour = now.getHours();
    const labels = [];
    const data = [];
    for (let i = Math.max(0, currentHour - 9); i <= currentHour; i++) {
        labels.push(`${i.toString().padStart(2, '0')}:00`);
        data.push(hourlyCounts[i]);
    }

    renderChart(labels, data);
    renderTopMissing(missingCounts);
}

// ── Rendering Functions ───────────────────────────────────────────────────────

function renderTimeline(slots) {
    const valid = slots.filter(s => s && s.job_id);
    if (valid.length === 0) {
        timelineFeed.innerHTML = '<p class="text-appleMuted text-sm text-center mt-10">Waiting for data...</p>';
        return;
    }

    timelineFeed.innerHTML = valid.map(slot => {
        const isMatch = slot.result === 'YES MATCH';
        const statusClass = isMatch ? 'status-match' : 'status-mismatch';
        const statusLabel = isMatch ? 'MATCH' : 'MISMATCH';

        const target = slot.target || {};
        const detected = slot.detected || {};

        const targetRows = Object.entries(target)
            .filter(([, v]) => v > 0)
            .map(([k, v]) => `
                <div class="flex justify-between items-center text-[11px] py-1 border-b border-white/5 last:border-0">
                    <span class="text-appleMuted font-medium truncate">${escapeHtml(k)}</span>
                    <span class="text-white font-mono ml-2 shrink-0">×${v}</span>
                </div>`).join('');

        const detectedRows = Object.entries(target)
            .filter(([, v]) => v > 0) // Show all fields that were expected
            .map(([k, targetVal]) => {
                const detectedVal = detected[k] || 0;
                const ok = detectedVal === targetVal;
                const colorClass = ok ? 'text-green-400' : 'text-red-400 font-bold';
                return `
                <div class="flex justify-between items-center text-[11px] py-1 border-b border-white/5 last:border-0">
                    <span class="text-appleMuted font-medium truncate">${escapeHtml(k)}</span>
                    <span class="${colorClass} font-mono ml-2 shrink-0">×${detectedVal}</span>
                </div>`;
            }).join('');

        return `
            <div class="p-5 glass-card rounded-2xl animate-fade-in group">
                <div class="flex items-center justify-between mb-4">
                    <div class="flex flex-col min-w-0">
                        <span class="text-[10px] uppercase tracking-widest text-appleMuted font-bold mb-0.5">Operation ID</span>
                        <span class="text-sm font-semibold text-white truncate px-1">${escapeHtml(slot.job_id)}</span>
                    </div>
                    <div class="flex flex-col items-end shrink-0 ml-4">
                        <span class="status-pill ${statusClass} mb-1.5">${statusLabel}</span>
                        <span class="text-[10px] text-appleMuted font-mono bg-white/5 px-2 py-0.5 rounded">${escapeHtml(slot.scanned_at || '—')}</span>
                    </div>
                </div>
                
                <div class="grid grid-cols-2 gap-6 mt-4">
                    <div class="bg-white/[0.03] p-3 rounded-xl border border-white/5">
                        <div class="flex items-center gap-2 mb-2">
                            <div class="w-1.5 h-1.5 rounded-full bg-blue-400 shadow-[0_0_8px_rgba(41,151,255,0.4)]"></div>
                            <p class="text-[10px] font-bold uppercase tracking-widest text-appleMuted">Data Info</p>
                        </div>
                        <div class="space-y-0.5">
                            ${targetRows || '<p class="text-[11px] text-appleMuted italic">No target data</p>'}
                        </div>
                    </div>
                    <div class="bg-white/[0.03] p-3 rounded-xl border border-white/5">
                        <div class="flex items-center gap-2 mb-2">
                            <div class="w-1.5 h-1.5 rounded-full ${isMatch ? 'bg-green-400 shadow-[0_0_8px_rgba(52,199,89,0.4)]' : 'bg-red-400 shadow-[0_0_8px_rgba(255,59,48,0.4)]'}"></div>
                            <p class="text-[10px] font-bold uppercase tracking-widest text-appleMuted">Tray Info</p>
                        </div>
                        <div class="space-y-0.5">
                            ${detectedRows || '<p class="text-[11px] text-appleMuted italic">No detection</p>'}
                        </div>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function renderTopMissing(missingCounts) {
    const sortedMissing = Object.entries(missingCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5); // Top 5

    if (sortedMissing.length === 0) {
        missingWidget.innerHTML = '<p class="text-appleMuted text-sm text-center mt-10">No errors recorded today.</p>';
        return;
    }

    missingWidget.innerHTML = sortedMissing.map(([itemName, count], index) => {
        const bgColors = ['bg-red-500/15', 'bg-orange-500/15', 'bg-yellow-500/15', 'bg-white/5', 'bg-white/5'];
        const textColors = ['text-red-400', 'text-orange-400', 'text-yellow-400', 'text-gray-400', 'text-gray-400'];
        const borderColors = ['border-red-500/20', 'border-orange-500/20', 'border-yellow-500/20', 'border-white/5', 'border-white/5'];

        return `
            <div class="flex items-center justify-between p-4 bg-white/[0.03] border ${borderColors[index]} rounded-2xl hover:bg-white/[0.06] transition-all group">
                <div class="flex items-center gap-4 min-w-0">
                    <span class="text-xs font-bold text-white/20 group-hover:text-white/40 transition-colors w-4">${index + 1}</span>
                    <span class="text-sm font-semibold truncate leading-none pt-0.5">${escapeHtml(itemName)}</span>
                </div>
                <div class="px-3 py-1.5 ${bgColors[index]} ${textColors[index]} rounded-xl font-bold text-[10px] uppercase tracking-wider shrink-0 border border-current/10">
                    ${count} missing
                </div>
            </div>
        `;
    }).join('');
}

function renderChart(labels, data) {
    const ctx = document.getElementById('throughputChart').getContext('2d');

    if (throughputChart) {
        throughputChart.data.labels = labels;
        throughputChart.data.datasets[0].data = data;
        throughputChart.update();
        return;
    }

    Chart.defaults.color = '#86868b';
    Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "SF Pro Display", sans-serif';

    throughputChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Inspections per Hour',
                data: data,
                backgroundColor: '#2997ff',
                borderRadius: 4,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { precision: 0 } },
                x: { grid: { display: false } }
            },
            plugins: {
                legend: { display: false },
                tooltip: { backgroundColor: 'rgba(28,28,30,0.9)', titleColor: '#fff', bodyColor: '#fff', padding: 10, cornerRadius: 8 }
            },
            animation: { duration: 400 }
        }
    });
}

// ── Listeners ─────────────────────────────────────────────────────────────────

function initListener() {
    const q = query(collection(db, "sync_events"), orderBy("timestamp", "desc"), limit(500));
    unsubscribeSnapshot = onSnapshot(q, (snapshot) => {
        const eventsData = [];
        snapshot.forEach((doc) => {
            eventsData.push({ id: doc.id, ...doc.data() });
        });
        processData(eventsData);
    }, (error) => {
        console.error("Firestore Listen Error:", error);
    });
}

function initInspectionLogListener() {
    const docRef = doc(db, "inspection_log", "rpi");
    unsubscribeInspectionLog = onSnapshot(docRef, (snap) => {
        if (!snap.exists()) {
            renderTimeline([]);
            return;
        }
        const data = snap.data();
        const slots = (data.slots || []).filter(s => s && s.job_id);
        // Sort by logged_at descending so newest is first
        slots.sort((a, b) => (b.logged_at || '').localeCompare(a.logged_at || ''));
        renderTimeline(slots);
    }, (error) => {
        console.error("Inspection Log Listen Error:", error);
    });
}

// ── Projects Tab ───────────────────────────────────────────────────────────────

function initProjectsListeners() {
    const refresh = () => renderCompanyProjects();

    unsubscribeProjects = onSnapshot(
        query(collection(db, 'projects'), orderBy('created_at', 'desc')),
        snap => { companyProjectsData = snap.docs.map(d => ({ id: d.id, ...d.data() })); refresh(); },
        err => console.error('Projects error:', err)
    );
    unsubscribeCustomers = onSnapshot(
        query(collection(db, 'customers'), orderBy('name')),
        snap => { companyCustomersData = snap.docs.map(d => ({ id: d.id, ...d.data() })); refresh(); },
        err => console.error('Customers error:', err)
    );
    unsubscribeDevices = onSnapshot(
        query(collection(db, 'devices'), orderBy('created_at', 'desc')),
        snap => { companyDevicesData = snap.docs.map(d => ({ id: d.id, ...d.data() })); refresh(); },
        err => console.error('Devices error:', err)
    );
    unsubscribeModels = onSnapshot(
        query(collection(db, 'models'), orderBy('created_at', 'desc')),
        snap => { companyModelsData = snap.docs.map(d => ({ id: d.id, ...d.data() })); refresh(); },
        err => console.error('Models error:', err)
    );
    unsubscribeApplications = onSnapshot(
        query(collection(db, 'applications'), orderBy('created_at', 'desc')),
        snap => { companyApplicationsData = snap.docs.map(d => ({ id: d.id, ...d.data() })); refresh(); },
        err => console.error('Applications error:', err)
    );
}

// ── Project Detail (full-page panel) ──────────────────────────────────────────

let unsubscribeDetailOps = null;

function _switchToPanel(panelId) {
    document.querySelectorAll('[data-panel]').forEach(p => {
        p.style.display = p.dataset.panel === panelId ? '' : 'none';
    });
    const tc = document.getElementById('tab-content');
    if (tc) tc.scrollTop = 0;
}

window.openProjectDrawer = function openProjectDetail(projectId) {
    const p = companyProjectsData.find(x => x.id === projectId);
    if (!p) return;

    const status = p.status || 'active';
    const statusColor = STATUS_COLOR[status] || STATUS_COLOR.active;

    // Header
    document.getElementById('pd-project-name').textContent = p.name;
    document.getElementById('pd-status-dot').className = `w-2 h-2 rounded-full ${statusColor.dot} shrink-0`;
    const appBadge = document.getElementById('pd-app-badge');
    appBadge.textContent = p.app_id || '';
    appBadge.className = `status-badge border text-[9px] ${p.app_id ? 'bg-blue-500/10 text-blue-400 border-blue-500/20' : 'hidden'}`;
    const descEl = document.getElementById('pd-description');
    descEl.textContent = p.description || '';
    descEl.classList.toggle('hidden', !p.description);

    // Customer
    const customer = companyCustomersData.find(c => c.id === p.customer_id);
    document.getElementById('pd-customer').innerHTML = customer
        ? `<p class="text-sm font-medium text-white">${customer.name}</p>
           ${customer.contact_email || customer.email ? `<p class="text-xs text-gray-400 mt-0.5">${customer.contact_email || customer.email}</p>` : ''}
           ${customer.country ? `<p class="text-xs text-gray-600">${customer.country}</p>` : ''}`
        : '<p class="text-xs italic text-gray-600">No customer linked</p>';

    // Devices
    const deviceIds = Array.isArray(p.device_ids) ? p.device_ids : [];
    const devices = deviceIds.map(id => companyDevicesData.find(d => d.id === id)).filter(Boolean);
    document.getElementById('pd-devices').innerHTML = devices.length
        ? devices.map(d => `
            <div class="flex items-center gap-2">
                <span class="w-1.5 h-1.5 rounded-full bg-purple-400 shrink-0"></span>
                <span class="text-xs text-gray-200">${d.name || d.device_id || d.id}</span>
                ${d.type ? `<span class="text-[10px] text-gray-600">${d.type}</span>` : ''}
            </div>`).join('')
        : '<p class="text-xs italic text-gray-600">No devices linked</p>';

    // Application
    const application = companyApplicationsData.find(a => a.id === p.application_id);
    document.getElementById('pd-application').innerHTML = application
        ? `<p class="text-sm font-medium text-white">${application.name}</p>
           ${application.description ? `<p class="text-xs text-gray-400 mt-0.5">${application.description}</p>` : ''}
           ${application.remarks ? `<p class="text-[10px] text-gray-600 mt-1 italic">${application.remarks}</p>` : ''}`
        : '<p class="text-xs italic text-gray-600">No application linked</p>';

    // Switch to detail panel
    _switchToPanel('project-detail');

    // Reset dynamic areas
    ['pd-stat-total','pd-stat-rate','pd-stat-mismatches','pd-stat-today'].forEach(id => {
        document.getElementById(id).textContent = '—';
    });
    document.getElementById('pd-missing').innerHTML = '<p class="text-xs text-gray-600 italic">Loading...</p>';
    document.getElementById('pd-operations').innerHTML = '<p class="text-gray-500 text-sm text-center py-10">Loading...</p>';

    if (unsubscribeDetailOps) { unsubscribeDetailOps(); unsubscribeDetailOps = null; }

    if (deviceIds.length === 0) {
        document.getElementById('pd-operations').innerHTML =
            '<p class="text-gray-600 text-xs text-center py-10">No devices linked — link devices to this project to see operations.</p>';
        document.getElementById('pd-missing').innerHTML = '<p class="text-xs italic text-gray-600">—</p>';
        ['pd-stat-total','pd-stat-rate','pd-stat-mismatches','pd-stat-today'].forEach(id => {
            document.getElementById(id).textContent = '0';
        });
        return;
    }

    const q = query(
        collection(db, 'sync_events'),
        where('device_id', 'in', deviceIds.slice(0, 30)),
        orderBy('timestamp', 'desc'),
        limit(100)
    );

    unsubscribeDetailOps = onSnapshot(q, snap => {
        const ops = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        renderDetailOperations(ops);
    }, err => {
        console.error('Detail ops error:', err);
        document.getElementById('pd-operations').innerHTML =
            '<p class="text-gray-600 text-xs text-center py-10">Could not load operations — index may still be building (1–2 min).</p>';
    });
};

function renderDetailOperations(ops) {
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const todayOps = ops.filter(o => o.timestamp?.toDate && o.timestamp.toDate().getTime() >= startOfToday);
    const mismatches = ops.filter(o => o.event_type === 'mismatch' || o.event_type === 'alert');
    const rate = ops.length > 0 ? Math.round(((ops.length - mismatches.length) / ops.length) * 100) : null;

    document.getElementById('pd-stat-total').textContent = ops.length;
    document.getElementById('pd-stat-rate').textContent = rate !== null ? `${rate}%` : '—';
    document.getElementById('pd-stat-mismatches').textContent = mismatches.length;
    document.getElementById('pd-stat-today').textContent = todayOps.length;

    // Top missing items from mismatch events
    const missingCounts = {};
    mismatches.forEach(op => {
        (op.missing_items || []).forEach(m => {
            missingCounts[m.class_name] = (missingCounts[m.class_name] || 0) + (m.count || 1);
        });
    });
    const topMissing = Object.entries(missingCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);
    const colors = ['text-red-400','text-orange-400','text-yellow-400','text-gray-400','text-gray-500'];
    document.getElementById('pd-missing').innerHTML = topMissing.length
        ? topMissing.map(([name, count], i) =>
            `<div class="flex items-center justify-between py-0.5">
                <span class="text-xs ${colors[i]}">${name}</span>
                <span class="text-xs font-mono text-gray-400">${count}×</span>
            </div>`).join('')
        : '<p class="text-xs italic text-gray-600">No missing items recorded</p>';

    // Operations list
    const opsEl = document.getElementById('pd-operations');
    if (ops.length === 0) {
        opsEl.innerHTML = '<p class="text-gray-600 text-sm text-center py-10">No operations found for this project.</p>';
        return;
    }
    opsEl.innerHTML = ops.slice(0, 50).map(op => {
        const isMismatch = op.event_type === 'mismatch' || op.event_type === 'alert';
        const statusCls = isMismatch ? 'bg-red-500/15 text-red-400 border-red-500/20' : 'bg-green-500/15 text-green-400 border-green-500/20';
        const dotCls = isMismatch ? 'bg-red-400' : 'bg-green-400';
        const label = isMismatch ? 'Mismatch' : 'Match';
        const ts = op.timestamp?.toDate
            ? op.timestamp.toDate().toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
            : '—';
        const missing = Array.isArray(op.missing_items) && op.missing_items.length
            ? `<div class="mt-2 flex flex-wrap gap-1">
                ${op.missing_items.map(m => `<span class="text-[9px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/15">${m.class_name} ×${m.count}</span>`).join('')}
               </div>`
            : '';
        return `
        <div class="bg-white/[0.02] border border-white/5 rounded-xl p-3.5 hover:border-white/10 transition-colors">
            <div class="flex items-start justify-between gap-3 mb-1.5">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="w-1.5 h-1.5 rounded-full ${dotCls} shrink-0"></span>
                    <span class="text-xs font-medium text-white truncate">${op.job_id || op.id}</span>
                </div>
                <span class="status-badge border ${statusCls} text-[9px] shrink-0">${label}</span>
            </div>
            <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-3.5 text-[10px] text-gray-500">
                <span>${ts}</span>
                ${op.device_id ? `<span class="font-mono text-gray-600">${op.device_id}</span>` : ''}
                ${op.expected_count != null ? `<span>Expected <b class="text-gray-300">${op.expected_count}</b> · Got <b class="${isMismatch ? 'text-red-400' : 'text-green-400'}">${op.actual_count ?? '—'}</b></span>` : ''}
            </div>
            ${missing}
        </div>`;
    }).join('');
}

document.getElementById('back-to-projects')?.addEventListener('click', () => {
    if (unsubscribeDetailOps) { unsubscribeDetailOps(); unsubscribeDetailOps = null; }
    _switchToPanel('projects');
});

const STATUS_COLOR = {
    active:   { dot: 'bg-green-400', badge: 'bg-green-500/15 text-green-400 border-green-500/20' },
    paused:   { dot: 'bg-amber-400',  badge: 'bg-amber-500/15 text-amber-400 border-amber-500/20' },
    archived: { dot: 'bg-gray-500',   badge: 'bg-gray-500/15 text-gray-400 border-gray-500/20' },
};

function renderCompanyProjects() {
    const container = document.getElementById('company-projects-list');
    const summary = document.getElementById('projects-summary');
    if (!container) return;

    const total = companyProjectsData.length;
    const active = companyProjectsData.filter(p => (p.status || 'active') === 'active').length;

    if (summary) {
        summary.innerHTML = `
            <span class="text-sm font-semibold text-white">${total}</span>
            <span class="text-xs text-gray-500">total</span>
            <span class="mx-2 text-gray-700">·</span>
            <span class="w-1.5 h-1.5 rounded-full bg-green-400 inline-block"></span>
            <span class="text-xs text-gray-400">${active} active</span>`;
    }

    if (total === 0) {
        container.innerHTML = `
            <div class="glass-card rounded-2xl p-12 flex flex-col items-center gap-3 col-span-full text-gray-500">
                <svg class="w-10 h-10 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>
                </svg>
                <span class="text-sm font-medium">No projects found</span>
            </div>`;
        return;
    }

    container.innerHTML = companyProjectsData.map(p => {
        const status = p.status || 'active';
        const statusColor = STATUS_COLOR[status] || STATUS_COLOR.active;

        const customer = companyCustomersData.find(c => c.id === p.customer_id);
        const devices = (Array.isArray(p.device_ids) ? p.device_ids : [])
            .map(id => companyDevicesData.find(d => d.id === id))
            .filter(Boolean);
        const model = companyModelsData.find(m => m.id === p.model_id);
        const application = companyApplicationsData.find(a => a.id === p.application_id);

        const created = p.created_at?.toDate
            ? p.created_at.toDate().toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
            : '—';

        const deviceItems = devices.length
            ? devices.map(d => `
                <div class="flex items-center gap-1.5">
                    <span class="w-1 h-1 rounded-full bg-purple-400 shrink-0"></span>
                    <span class="text-xs text-gray-300 truncate">${d.name || d.device_id || d.id}</span>
                    ${d.type ? `<span class="text-[9px] text-gray-600">${d.type}</span>` : ''}
                </div>`).join('')
            : '<span class="text-xs italic text-gray-600">None linked</span>';

        return `
        <div class="glass-card rounded-2xl p-5 flex flex-col gap-4 hover:border-white/20 transition-all cursor-pointer" onclick="openProjectDrawer('${p.id}')" role="button">

            <!-- Header -->
            <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                    <div class="flex items-center gap-2 mb-1">
                        <span class="w-2 h-2 rounded-full ${statusColor.dot} shrink-0"></span>
                        <span class="font-semibold text-white text-sm truncate">${p.name}</span>
                    </div>
                    ${p.description ? `<p class="text-[11px] text-gray-500 truncate pl-4">${p.description}</p>` : ''}
                </div>
                <span class="status-badge border ${statusColor.badge} shrink-0">${status}</span>
            </div>

            <!-- Info rows -->
            <div class="space-y-3 border-t border-white/5 pt-4">

                <!-- Customer -->
                <div class="flex gap-3">
                    <div class="w-5 h-5 rounded-md bg-[#0a84ff]/15 flex items-center justify-center shrink-0 mt-0.5">
                        <svg class="w-3 h-3 text-[#0a84ff]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-2 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/></svg>
                    </div>
                    <div class="min-w-0">
                        <p class="text-[9px] font-bold uppercase tracking-widest text-gray-600 mb-0.5">Customer</p>
                        ${customer
                            ? `<p class="text-xs text-gray-200">${customer.name}</p>
                               ${customer.contact_email || customer.email ? `<p class="text-[10px] text-gray-500">${customer.contact_email || customer.email}</p>` : ''}`
                            : '<p class="text-xs italic text-gray-600">—</p>'}
                    </div>
                </div>

                <!-- Devices -->
                <div class="flex gap-3">
                    <div class="w-5 h-5 rounded-md bg-purple-500/15 flex items-center justify-center shrink-0 mt-0.5">
                        <svg class="w-3 h-3 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18"/></svg>
                    </div>
                    <div class="min-w-0 flex-1">
                        <p class="text-[9px] font-bold uppercase tracking-widest text-gray-600 mb-0.5">Devices</p>
                        <div class="space-y-0.5">${deviceItems}</div>
                    </div>
                </div>

                <!-- AI Model -->
                <div class="flex gap-3">
                    <div class="w-5 h-5 rounded-md bg-emerald-500/15 flex items-center justify-center shrink-0 mt-0.5">
                        <svg class="w-3 h-3 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>
                    </div>
                    <div class="min-w-0">
                        <p class="text-[9px] font-bold uppercase tracking-widest text-gray-600 mb-0.5">AI Model</p>
                        ${model
                            ? `<p class="text-xs text-gray-200">${model.name}</p>
                               <p class="text-[10px] text-gray-500 font-mono">v${model.version} · ${model.framework || ''}</p>`
                            : '<p class="text-xs italic text-gray-600">—</p>'}
                    </div>
                </div>

                <!-- Application -->
                <div class="flex gap-3">
                    <div class="w-5 h-5 rounded-md bg-orange-500/15 flex items-center justify-center shrink-0 mt-0.5">
                        <svg class="w-3 h-3 text-orange-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>
                    </div>
                    <div class="min-w-0">
                        <p class="text-[9px] font-bold uppercase tracking-widest text-gray-600 mb-0.5">Application</p>
                        ${application
                            ? `<p class="text-xs text-gray-200">${application.name}</p>
                               ${application.description ? `<p class="text-[10px] text-gray-500">${application.description}</p>` : ''}`
                            : '<p class="text-xs italic text-gray-600">—</p>'}
                    </div>
                </div>

            </div>

            <!-- Footer -->
            <div class="border-t border-white/5 pt-3 flex items-center justify-between">
                ${p.app_id ? `<span class="text-[9px] font-bold uppercase tracking-widest px-2 py-0.5 rounded-full border bg-blue-500/10 text-blue-400 border-blue-500/20">${p.app_id}</span>` : '<span></span>'}
                <span class="text-[10px] text-gray-600 font-mono">${created}</span>
            </div>

        </div>`;
    }).join('');
}
