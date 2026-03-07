import { getFirestore, collection, query, orderBy, limit, onSnapshot, doc } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js";
import { app } from "./firebase-config.js";
import { setupAuthUI, getCurrentUser } from "./auth.js";

const db = getFirestore(app);

// DOM Elements
const authOverlay = document.getElementById('auth-overlay');
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

// Auth Callbacks
function requireLogin() {
    authOverlay.classList.remove('hidden');
    mainContent.classList.add('hidden');
    userEmailDisplay.innerText = '';
    if (unsubscribeSnapshot) { unsubscribeSnapshot(); unsubscribeSnapshot = null; }
    if (unsubscribeInspectionLog) { unsubscribeInspectionLog(); unsubscribeInspectionLog = null; }
}

function loggedIn(user) {
    authOverlay.classList.add('hidden');
    mainContent.classList.remove('hidden');
    userEmailDisplay.innerText = user.email;
    initListener();
    initInspectionLogListener();
}

setupAuthUI(requireLogin, loggedIn);

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
                    <span class="text-appleMuted font-medium truncate">${k}</span>
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
                    <span class="text-appleMuted font-medium truncate">${k}</span>
                    <span class="${colorClass} font-mono ml-2 shrink-0">×${detectedVal}</span>
                </div>`;
            }).join('');

        return `
            <div class="p-5 glass-card rounded-2xl animate-fade-in group">
                <div class="flex items-center justify-between mb-4">
                    <div class="flex flex-col min-w-0">
                        <span class="text-[10px] uppercase tracking-widest text-appleMuted font-bold mb-0.5">Operation ID</span>
                        <span class="text-sm font-semibold text-white truncate px-1">${slot.job_id}</span>
                    </div>
                    <div class="flex flex-col items-end shrink-0 ml-4">
                        <span class="status-pill ${statusClass} mb-1.5">${statusLabel}</span>
                        <span class="text-[10px] text-appleMuted font-mono bg-white/5 px-2 py-0.5 rounded">${slot.scanned_at || '—'}</span>
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
                    <span class="text-sm font-semibold truncate leading-none pt-0.5">${itemName}</span>
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
