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
        const dotColor = isMatch
            ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.5)]'
            : 'bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.5)]';
        const badge = isMatch
            ? '<span class="text-xs font-semibold text-green-400 bg-green-500/10 px-2 py-0.5 rounded-md">MATCH</span>'
            : '<span class="text-xs font-semibold text-red-400 bg-red-500/10 px-2 py-0.5 rounded-md">MISMATCH</span>';

        const target = slot.target || {};
        const detected = slot.detected || {};
        const targetRows = Object.entries(target)
            .filter(([, v]) => v > 0)
            .map(([k, v]) => `
                <div class="flex justify-between text-xs py-0.5">
                    <span class="text-gray-400 truncate">${k}</span>
                    <span class="text-white ml-2 shrink-0">× ${v}</span>
                </div>`).join('');
        const detectedRows = Object.entries(detected)
            .map(([k, v]) => {
                const expected = target[k] || 0;
                const ok = v === expected && expected > 0;
                const color = ok ? 'text-green-400' : 'text-red-400';
                return `
                <div class="flex justify-between text-xs py-0.5">
                    <span class="text-gray-400 truncate">${k}</span>
                    <span class="${color} ml-2 shrink-0">× ${v}</span>
                </div>`;
            }).join('');

        return `
            <div class="p-3 bg-white/5 rounded-xl border border-white/5 hover:bg-white/8 transition-colors animate-fade-in">
                <div class="flex items-center justify-between mb-2">
                    <div class="flex items-center gap-2 min-w-0">
                        <div class="w-2 h-2 rounded-full shrink-0 ${dotColor}"></div>
                        <span class="text-sm font-medium text-white truncate">${slot.job_id}</span>
                    </div>
                    <div class="flex items-center gap-2 shrink-0 ml-2">
                        <span class="text-xs text-appleMuted font-mono">${slot.scanned_at || '—'}</span>
                        ${badge}
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-3 mt-2 pt-2 border-t border-white/5">
                    <div>
                        <p class="text-xs uppercase tracking-wider text-appleMuted mb-1">DATA INFO</p>
                        ${targetRows || '<span class="text-xs text-appleMuted">—</span>'}
                    </div>
                    <div>
                        <p class="text-xs uppercase tracking-wider text-appleMuted mb-1">TRAY INFO</p>
                        ${detectedRows || '<span class="text-xs text-appleMuted">—</span>'}
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
        const bgColors = ['bg-red-500/20', 'bg-orange-500/20', 'bg-yellow-500/20', 'bg-white/10', 'bg-white/10'];
        const textColors = ['text-red-400', 'text-orange-400', 'text-yellow-400', 'text-appleMuted', 'text-appleMuted'];

        return `
            <div class="flex items-center justify-between p-3 border-b border-white/5 last:border-0 hover:bg-white/5 transition-colors rounded-lg">
                <div class="flex items-center gap-3 truncate">
                    <span class="text-xs font-bold text-gray-500 w-4">${index + 1}</span>
                    <span class="text-sm font-medium truncate">${itemName}</span>
                </div>
                <div class="px-2.5 py-1 ${bgColors[index]} ${textColors[index]} rounded font-bold text-xs shrink-0">
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
