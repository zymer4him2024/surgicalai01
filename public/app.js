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
        const isMatch = slot.result === 'GOOD' || slot.result === 'YES MATCH';
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
                        <span class="text-[10px] text-appleMuted font-mono bg-white/5 px-2 py-0.5 rounded">${escapeHtml(slot.scanned_at || (slot.timestamp?.toDate ? slot.timestamp.toDate().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'}) : (slot.logged_at ? new Date(slot.logged_at).toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'}) : '—')))}</span>
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
    let opsData = [];
    let logData = [];

    function mergeAndRender() {
        // If operations collection has data, use it; otherwise fall back to inspection_log
        const combined = opsData.length > 0 ? opsData : logData;
        renderTimeline(combined);
    }

    // Listen to operations collection (unbounded history)
    const opsQ = query(collection(db, "operations"), orderBy("timestamp", "desc"), limit(50));
    const unsubOps = onSnapshot(opsQ, (snapshot) => {
        opsData = snapshot.docs.map(d => ({ id: d.id, ...d.data() })).filter(s => s.job_id);
        mergeAndRender();
    }, (error) => {
        console.error("Operations Listen Error:", error);
    });

    // Listen to inspection_log (circular buffer fallback)
    const logCol = collection(db, "inspection_log");
    const unsubLog = onSnapshot(logCol, (snapshot) => {
        const allSlots = [];
        snapshot.forEach((docSnap) => {
            const data = docSnap.data();
            const s = (data.slots || []).filter(s => s && s.job_id);
            allSlots.push(...s);
        });
        allSlots.sort((a, b) => (b.logged_at || '').localeCompare(a.logged_at || ''));
        logData = allSlots;
        mergeAndRender();
    }, (error) => {
        console.error("Inspection Log Listen Error:", error);
    });

    unsubscribeInspectionLog = () => { unsubOps(); unsubLog(); };
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
let _unsubscribeDetailLog = null;

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
    if (_unsubscribeDetailLog) { _unsubscribeDetailLog(); _unsubscribeDetailLog = null; }

    if (deviceIds.length === 0) {
        document.getElementById('pd-operations').innerHTML =
            '<p class="text-gray-600 text-xs text-center py-10">No devices linked — link devices to this project to see operations.</p>';
        document.getElementById('pd-missing').innerHTML = '<p class="text-xs italic text-gray-600">—</p>';
        ['pd-stat-total','pd-stat-rate','pd-stat-mismatches','pd-stat-today'].forEach(id => {
            document.getElementById(id).textContent = '0';
        });
        return;
    }

    // Track all three data sources and merge
    let syncOps = [];
    let opsData = [];
    let logData = [];
    function mergeAndRender() {
        // Prefer operations collection, fall back to inspection_log
        const logSlots = opsData.length > 0 ? opsData : logData;
        renderDetailOperations(syncOps, logSlots);
    }

    const unsubs = [];

    // 1) sync_events filtered by device_id
    const q = query(
        collection(db, 'sync_events'),
        where('device_id', 'in', deviceIds.slice(0, 30)),
        orderBy('timestamp', 'desc'),
        limit(100)
    );
    unsubs.push(onSnapshot(q, snap => {
        syncOps = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        mergeAndRender();
    }, err => {
        console.error('Detail sync_events error:', err);
        mergeAndRender();
    }));

    // 2) operations collection for linked devices
    const opsQ = query(
        collection(db, 'operations'),
        where('device_id', 'in', deviceIds.slice(0, 30)),
        orderBy('timestamp', 'desc'),
        limit(100)
    );
    unsubs.push(onSnapshot(opsQ, snap => {
        opsData = snap.docs.map(d => ({ id: d.id, ...d.data(), _source: 'log', _device_id: d.data().device_id })).filter(s => s.job_id);
        mergeAndRender();
    }, err => {
        console.error('Detail operations error:', err);
    }));

    // 3) inspection_log fallback for linked devices
    const logRefs = deviceIds.map(id => doc(db, 'inspection_log', id));
    logRefs.forEach(ref => {
        unsubs.push(onSnapshot(ref, snap => {
            const devId = snap.id;
            logData = logData.filter(s => s._device_id !== devId);
            if (snap.exists()) {
                const data = snap.data();
                const slots = (data.slots || []).filter(s => s && s.job_id).map(s => ({ ...s, _source: 'log', _device_id: devId }));
                logData.push(...slots);
            }
            mergeAndRender();
        }, err => {
            console.error('Detail inspection_log error:', err);
        }));
    });

    unsubscribeDetailOps = () => unsubs.forEach(u => u());
    _unsubscribeDetailLog = null;
};

function renderDetailOperations(syncOps, logSlots) {
    // Convert inspection_log slots to a unified format
    const logOps = (logSlots || []).map(s => {
        const isMatch = s.result === 'GOOD' || s.result === 'YES MATCH';
        return {
            _source: 'log',
            job_id: s.job_id,
            event_type: isMatch ? 'match' : 'mismatch',
            device_id: s._device_id || s.device_id || '',
            logged_at: s.logged_at || '',
            target: s.target,
            detected: s.detected,
        };
    });

    // Merge: sync_events first (they have timestamps), then log slots
    const all = [...syncOps, ...logOps];

    // Sort: sync_events by timestamp, log by logged_at
    all.sort((a, b) => {
        const ta = a.timestamp?.toDate ? a.timestamp.toDate().getTime() : new Date(a.logged_at || 0).getTime();
        const tb = b.timestamp?.toDate ? b.timestamp.toDate().getTime() : new Date(b.logged_at || 0).getTime();
        return tb - ta;
    });

    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const todayOps = all.filter(o => {
        const t = o.timestamp?.toDate ? o.timestamp.toDate().getTime() : new Date(o.logged_at || 0).getTime();
        return t >= startOfToday;
    });
    const mismatches = all.filter(o => o.event_type === 'mismatch' || o.event_type === 'alert');
    const rate = all.length > 0 ? Math.round(((all.length - mismatches.length) / all.length) * 100) : null;

    document.getElementById('pd-stat-total').textContent = all.length;
    document.getElementById('pd-stat-rate').textContent = rate !== null ? `${rate}%` : '—';
    document.getElementById('pd-stat-mismatches').textContent = mismatches.length;
    document.getElementById('pd-stat-today').textContent = todayOps.length;

    // Top missing items from mismatch events
    const missingCounts = {};
    mismatches.forEach(op => {
        (op.missing_items || []).forEach(m => {
            missingCounts[m.class_name] = (missingCounts[m.class_name] || 0) + (m.count || 1);
        });
        // Also diff target vs detected from inspection_log slots
        if (op._source === 'log' && op.target && op.detected) {
            Object.entries(op.target).forEach(([k, v]) => {
                const det = op.detected[k] || 0;
                if (det < v) {
                    missingCounts[k] = (missingCounts[k] || 0) + (v - det);
                }
            });
        }
    });
    const topMissing = Object.entries(missingCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);
    const colors = ['text-red-400','text-orange-400','text-yellow-400','text-gray-400','text-gray-500'];
    document.getElementById('pd-missing').innerHTML = topMissing.length
        ? topMissing.map(([name, count], i) =>
            `<div class="flex items-center justify-between py-0.5">
                <span class="text-xs ${colors[i]}">${escapeHtml(name)}</span>
                <span class="text-xs font-mono text-gray-400">${count}×</span>
            </div>`).join('')
        : '<p class="text-xs italic text-gray-600">No missing items recorded</p>';

    // Operations list
    const opsEl = document.getElementById('pd-operations');
    if (all.length === 0) {
        opsEl.innerHTML = '<p class="text-gray-600 text-sm text-center py-10">No operations found for this project.</p>';
        return;
    }

    // Store for modal access
    window._detailOpsCache = all;

    opsEl.innerHTML = all.map((op, idx) => {
        const isMismatch = op.event_type === 'mismatch' || op.event_type === 'alert';
        const statusCls = isMismatch ? 'bg-red-500/15 text-red-400 border-red-500/20' : 'bg-green-500/15 text-green-400 border-green-500/20';
        const dotCls = isMismatch ? 'bg-red-400' : 'bg-green-400';
        const label = isMismatch ? 'Mismatch' : 'Match';
        const ts = op.timestamp?.toDate
            ? op.timestamp.toDate().toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
            : op.logged_at
            ? new Date(op.logged_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
            : '—';
        return `
        <div class="op-row bg-white/[0.02] border border-white/5 rounded-xl p-3.5 hover:border-white/10 hover:bg-white/[0.04] transition-colors cursor-pointer" data-op-idx="${idx}">
            <div class="flex items-start justify-between gap-3 mb-1.5">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="w-1.5 h-1.5 rounded-full ${dotCls} shrink-0"></span>
                    <span class="text-xs font-medium text-white truncate">${escapeHtml(op.job_id || op.id)}</span>
                </div>
                <span class="status-badge border ${statusCls} text-[9px] shrink-0">${label}</span>
            </div>
            <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-3.5 text-[10px] text-gray-500">
                <span>${ts}</span>
                ${op.device_id ? `<span class="font-mono text-gray-600">${escapeHtml(op.device_id)}</span>` : ''}
            </div>
        </div>`;
    }).join('');

    // Attach click handlers
    opsEl.querySelectorAll('.op-row').forEach(row => {
        row.addEventListener('click', () => {
            const idx = parseInt(row.dataset.opIdx, 10);
            const op = window._detailOpsCache[idx];
            if (op) _showOpModal(op);
        });
    });
}

document.getElementById('back-to-projects')?.addEventListener('click', () => {
    if (unsubscribeDetailOps) { unsubscribeDetailOps(); unsubscribeDetailOps = null; }
    _switchToPanel('projects');
});

// ── Operation Detail Modal ────────────────────────────────────────────────────

function _showOpModal(op) {
    const modal = document.getElementById('op-modal');
    const content = document.getElementById('op-modal-content');
    if (!modal || !content) return;

    const isMismatch = op.event_type === 'mismatch' || op.event_type === 'alert';
    const statusCls = isMismatch ? 'bg-red-500/15 text-red-400 border-red-500/20' : 'bg-green-500/15 text-green-400 border-green-500/20';
    const statusLabel = isMismatch ? 'Mismatch' : 'Match';
    const ts = op.timestamp?.toDate
        ? op.timestamp.toDate().toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : op.logged_at
        ? new Date(op.logged_at).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '—';

    // Build target (DATA INFO) and detected (TRAY INFO)
    const target = op.target || op.metadata?.target || {};
    const detected = op.detected || op.metadata?.detected || {};

    // For sync_events, detected_items is an array of {class_name, count}
    let detectedMap = detected;
    if (Array.isArray(op.detected_items) && op.detected_items.length && typeof detected !== 'object') {
        detectedMap = {};
        op.detected_items.forEach(d => {
            if (d.class_name) detectedMap[d.class_name] = (detectedMap[d.class_name] || 0) + (d.count || 1);
        });
    }

    // All keys from both target and detected
    const allKeys = [...new Set([...Object.keys(target), ...Object.keys(detectedMap)])].filter(k => k !== 'total');
    const totalTarget = target.total != null ? target.total : null;

    function buildRows(items, colorFn) {
        if (Object.keys(items).length === 0) return '<p class="text-xs italic text-gray-600 py-2">No data</p>';
        return Object.entries(items).filter(([k]) => k !== 'total').map(([k, v]) => `
            <div class="flex justify-between items-center py-2 border-b border-white/5 last:border-0">
                <span class="text-sm text-gray-300">${escapeHtml(k)}</span>
                <span class="text-sm font-mono ${colorFn ? colorFn(k, v) : 'text-white'}">${v}</span>
            </div>`).join('');
    }

    const targetRows = totalTarget != null
        ? `<div class="flex justify-between items-center py-2">
               <span class="text-sm text-gray-300">Total Objects</span>
               <span class="text-sm font-mono text-white font-bold">${totalTarget}</span>
           </div>`
        : buildRows(target, () => 'text-white');

    const detectedRows = totalTarget != null
        ? `<div class="flex justify-between items-center py-2">
               <span class="text-sm text-gray-300">Total Detected</span>
               <span class="text-sm font-mono font-bold ${Math.abs(Object.values(detectedMap).reduce((a,b)=>a+b,0) - totalTarget) <= 1 ? 'text-green-400' : 'text-red-400'}">${Object.values(detectedMap).reduce((a,b)=>a+b,0)}</span>
           </div>` + buildRows(detectedMap, (k, v) => 'text-gray-300')
        : buildRows(detectedMap, (k, v) => {
            const expected = target[k];
            if (expected == null) return 'text-gray-300';
            return v >= expected ? 'text-green-400' : 'text-red-400';
        });

    // Snapshot button (opens lightbox)
    const snapshots = op.snapshot_urls || [];
    const snapshotHtml = snapshots.length
        ? `<div class="mt-5 text-center">
               <button id="op-view-snaps-btn" class="inline-flex items-center gap-2 px-5 py-2.5 bg-red-500/10 border border-red-500/20 rounded-xl text-xs font-bold uppercase tracking-wider text-red-400 hover:bg-red-500/20 transition-all shadow-sm">
                   <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                   View Snapshots (${snapshots.length})
               </button>
           </div>`
        : '';

    content.innerHTML = `
        <div class="mb-5">
            <div class="flex items-center justify-between mb-1">
                <h3 class="text-lg font-semibold text-white">${escapeHtml(op.job_id || op.id || '—')}</h3>
                <span class="status-badge border ${statusCls} text-[10px]">${statusLabel}</span>
            </div>
            <div class="flex flex-wrap gap-3 text-[11px] text-gray-500">
                <span>${ts}</span>
                ${op.device_id ? `<span class="font-mono">${escapeHtml(op.device_id)}</span>` : ''}
            </div>
        </div>

        <div class="grid grid-cols-2 gap-4">
            <div class="bg-white/[0.03] rounded-xl p-4 border border-white/5">
                <div class="flex items-center gap-2 mb-3">
                    <div class="w-1.5 h-1.5 rounded-full bg-blue-400 shadow-[0_0_8px_rgba(41,151,255,0.4)]"></div>
                    <p class="text-[10px] font-bold uppercase tracking-widest text-gray-500">Data Info</p>
                </div>
                ${targetRows}
            </div>
            <div class="bg-white/[0.03] rounded-xl p-4 border border-white/5">
                <div class="flex items-center gap-2 mb-3">
                    <div class="w-1.5 h-1.5 rounded-full ${isMismatch ? 'bg-red-400 shadow-[0_0_8px_rgba(255,59,48,0.4)]' : 'bg-green-400 shadow-[0_0_8px_rgba(52,199,89,0.4)]'}"></div>
                    <p class="text-[10px] font-bold uppercase tracking-widest text-gray-500">Tray Info</p>
                </div>
                ${detectedRows}
            </div>
        </div>
        ${snapshotHtml}
    `;

    modal.classList.remove('hidden');

    // Attach snapshot lightbox handler
    const snapBtn = document.getElementById('op-view-snaps-btn');
    if (snapBtn && snapshots.length) {
        snapBtn.addEventListener('click', () => _openSnapLightbox(snapshots));
    }
}

function _openSnapLightbox(urls) {
    const lightbox = document.getElementById('snap-lightbox');
    const content = document.getElementById('snap-lightbox-content');
    if (!lightbox || !content) return;

    content.innerHTML = urls.map((url, i) => `
        <img src="${escapeHtml(url)}" class="w-full rounded-xl border border-white/10 shadow-lg" loading="lazy" alt="Snapshot ${i + 1}">
    `).join('');

    lightbox.classList.remove('hidden');
}

// Modal close handlers
document.getElementById('op-modal-close')?.addEventListener('click', () => {
    document.getElementById('op-modal')?.classList.add('hidden');
});
document.getElementById('op-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden');
});
document.getElementById('snap-lightbox-close')?.addEventListener('click', () => {
    document.getElementById('snap-lightbox')?.classList.add('hidden');
});
document.getElementById('snap-lightbox')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden');
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
