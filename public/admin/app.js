import { getFirestore, collection, query, orderBy, limit, onSnapshot, where, doc, setDoc, addDoc, deleteDoc, updateDoc, serverTimestamp, getDoc, writeBatch } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js";
import { getStorage, ref, uploadBytesResumable, getDownloadURL } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-storage.js";
import { app } from "../firebase-config.js";
import { setupAuthUI, getCurrentUser } from "../auth.js";

const db = getFirestore(app);
const storage = getStorage(app);

// Active device — null until user selects one from the devices panel
let ACTIVE_DEVICE_ID = null;

// DOM Elements
const authOverlay = document.getElementById('auth-overlay');
const mainContent = document.getElementById('main-content');
const errorTableBody = document.getElementById('error-table-body');
const userEmailDisplay = document.getElementById('user-email');
const modal = document.getElementById('snapshot-modal');
const closeModal = document.getElementById('close-modal');
const sliderContainer = document.getElementById('slider-container');

let unsubscribeSnapshot = null;
let unsubscribeControl = null;
let unsubscribeStatus = null;
let unsubscribeCustomers = null;
let unsubscribeDevices = null;
let unsubscribeOverview = [];
let statusAgeInterval = null;
let lastStatusUpdate = null;

// Auth Callbacks
function requireLogin() {
    authOverlay.classList.remove('hidden');
    mainContent.classList.add('hidden');
    userEmailDisplay.innerText = '';
    if (unsubscribeSnapshot) { unsubscribeSnapshot(); unsubscribeSnapshot = null; }
    if (unsubscribeControl) { unsubscribeControl(); unsubscribeControl = null; }
    if (unsubscribeStatus) { unsubscribeStatus(); unsubscribeStatus = null; }
    if (unsubscribeCustomers) { unsubscribeCustomers(); unsubscribeCustomers = null; }
    if (unsubscribeDevices) { unsubscribeDevices(); unsubscribeDevices = null; }
    if (unsubscribeProjects) { unsubscribeProjects(); unsubscribeProjects = null; }
    if (unsubscribeModels) { unsubscribeModels(); unsubscribeModels = null; }
    if (unsubscribeApplications) { unsubscribeApplications(); unsubscribeApplications = null; }
    if (unsubscribeAppTypes) { unsubscribeAppTypes(); unsubscribeAppTypes = null; }
    if (unsubscribeUsers) { unsubscribeUsers(); unsubscribeUsers = null; }
    unsubscribeOverview.forEach(u => u());
    unsubscribeOverview = [];
    if (statusAgeInterval) { clearInterval(statusAgeInterval); statusAgeInterval = null; }
}

function loggedIn(user) {
    // Admin page requires admin custom claim — approved-but-non-admin users are redirected
    user.getIdTokenResult().then(token => {
        if (!token.claims.admin) {
            requireLogin();
            return;
        }
        authOverlay.classList.add('hidden');
        mainContent.classList.remove('hidden');
        userEmailDisplay.innerText = user.email;
        initOverview();
        initAppTypes();
        initApplications();
        initModels();
        initProjects();
        initListener();
        initCustomers();
        initDevices();
        initUsers();
        // Status and control panels start idle — activated when user selects a device
    });
}

setupAuthUI(requireLogin, loggedIn);

// Modal Logic
closeModal.addEventListener('click', () => modal.classList.add('hidden'));

function openSnapshots(urls) {
    sliderContainer.innerHTML = '';
    if (urls && urls.length > 0) {
        urls.forEach(url => {
            const div = document.createElement('div');
            div.className = 'slider-item';
            const img = document.createElement('img');
            img.src = url;
            div.appendChild(img);
            sliderContainer.appendChild(div);
        });
    } else {
        sliderContainer.innerHTML = '<p class="text-gray-400 text-center w-full">No snapshots available.</p>';
    }
    modal.classList.remove('hidden');
}

// Data Processing
function processData(eventsData) {
    if (eventsData.length === 0) {
        errorTableBody.innerHTML = '<tr><td colspan="4" class="py-12 px-6 text-center text-appleMuted italic">No recent errors detected in the last 50 events.</td></tr>';
        return;
    }

    errorTableBody.innerHTML = '';
    eventsData.forEach(err => {
        const timeStr = err.timestamp ? new Date(err.timestamp.toMillis()).toLocaleString() : 'N/A';
        const jobId = err.metadata?.job_id || 'Unknown';
        const reason = err.metadata?.reason || err.event_type;
        const tr = document.createElement('tr');
        tr.className = 'group hover:bg-white/[0.03] transition-all border-b border-white/5 last:border-0';

        let btnHtml = '<span class="text-xs text-gray-600 font-medium">No Snapshots</span>';
        if (err.snapshot_urls && err.snapshot_urls.length > 0) {
            btnHtml = `<button class="view-btn px-4 py-1.5 bg-red-500/10 border border-red-500/20 rounded-xl text-[10px] font-bold uppercase tracking-wider text-red-400 hover:bg-red-500/20 transition-all shadow-sm">View Snaps</button>`;
        }

        tr.innerHTML = `
            <td class="py-4 px-6">
                <div class="flex flex-col">
                    <span class="text-white font-medium">${timeStr.split(', ')[1]}</span>
                    <span class="text-[10px] text-appleMuted">${timeStr.split(', ')[0]}</span>
                </div>
            </td>
            <td class="py-4 px-6 font-mono font-semibold text-blue-400/80 group-hover:text-blue-400 transition-colors">${jobId}</td>
            <td class="py-4 px-6">
                <span class="status-badge badge-error">${reason}</span>
            </td>
            <td class="py-4 px-6 text-right">${btnHtml}</td>
        `;

        if (err.snapshot_urls && err.snapshot_urls.length > 0) {
            const btn = tr.querySelector('.view-btn');
            btn.onclick = () => openSnapshots(err.snapshot_urls);
        }
        errorTableBody.appendChild(tr);
    });
}

// ── Live System Status Panel ──────────────────────────────────────────────────

const stateBadge = document.getElementById('state-badge');
const jobIdEl = document.getElementById('job-id');
const runningPill = document.getElementById('running-pill');
const targetTags = document.getElementById('target-tags');
const detectionList = document.getElementById('detection-list');
const statusAge = document.getElementById('status-age');

const STATE_STYLES = {
    MATCH: 'badge-match',
    ERROR: 'badge-error',
    READY: 'badge-ready',
};

function renderStatusCard(data) {
    const state = data.system_state || '—';
    const job = data.current_job;
    const detections = data.latest_detections || [];
    const running = data.inference_running;

    // State badge
    stateBadge.className = `status-badge ${STATE_STYLES[state] || 'bg-gray-700 text-gray-300'} text-sm px-6 py-2 transition-all duration-500 shadow-lg`;
    stateBadge.textContent = state;

    // Running pill
    runningPill.className = `px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider ${running ? 'bg-green-500/15 text-green-400 border border-green-500/20' : 'bg-red-500/15 text-red-400 border border-red-500/20'}`;
    runningPill.textContent = running ? 'System Active' : 'System Paused';

    // Job info
    if (job) {
        jobIdEl.textContent = job.id || 'Unknown';
        const target = job.target || {};
        targetTags.innerHTML = Object.entries(target)
            .map(([k, v]) => `<span class="px-2 py-1 bg-white/[0.05] border border-white/10 rounded-lg text-[10px] font-mono text-gray-300">${k} ×${v}</span>`)
            .join('');
    } else {
        jobIdEl.textContent = 'No active job';
        targetTags.innerHTML = '';
    }

    // Detection list
    if (detections.length > 0) {
        detectionList.innerHTML = detections.map(d => {
            const fdaBadge = d.fda_class
                ? `<span class="px-2 py-0.5 bg-blue-500/15 text-blue-400 border border-blue-500/20 rounded-lg text-[9px] font-bold uppercase">${d.fda_class}</span>`
                : '';
            const name = d.device_name || d.class_name;
            return `<div class="flex items-center gap-4 py-2.5 border-b border-white/5 last:border-0 group">
                <span class="font-mono text-xs w-28 shrink-0 text-white/60 group-hover:text-white transition-colors uppercase tracking-tighter">${d.class_name}</span>
                <span class="text-white font-bold w-10 shrink-0 text-base">×${d.count}</span>
                ${fdaBadge}
                <span class="text-appleMuted text-xs truncate font-medium ml-1">${name}</span>
            </div>`;
        }).join('');
    } else {
        detectionList.innerHTML = '<p class="text-sm text-appleMuted italic py-4">Waiting for real-time detections...</p>';
    }
}

// ── Overview Summary Cards ────────────────────────────────────────────────────

function initOverview() {
    unsubscribeOverview.forEach(u => u());
    unsubscribeOverview = [];

    const container = document.getElementById('overview-cards');
    if (!container) return;

    // State tracked per card
    const counts = { projects: '—', customers: '—', devices: '—', alerts: '—', devicesOnline: '—' };

    function renderCards() {
        container.innerHTML = [
            {
                label: 'Projects',
                value: counts.projects,
                sub: 'total registered',
                color: 'text-[#0a84ff]',
                bg: 'bg-blue-500/10',
                border: 'border-blue-500/20',
                icon: `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>`,
            },
            {
                label: 'Customers',
                value: counts.customers,
                sub: 'organizations',
                color: 'text-blue-400',
                bg: 'bg-blue-400/10',
                border: 'border-blue-400/20',
                icon: `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/>`,
            },
            {
                label: 'Devices',
                value: counts.devicesOnline,
                sub: `of ${counts.devices} online`,
                color: 'text-[#34c759]',
                bg: 'bg-green-500/10',
                border: 'border-green-500/20',
                icon: `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z"/>`,
            },
            {
                label: 'Recent Alerts',
                value: counts.alerts,
                sub: 'last 50 events',
                color: 'text-[#ff3b30]',
                bg: 'bg-red-500/10',
                border: 'border-red-500/20',
                icon: `<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>`,
            },
        ].map(card => `
            <div class="glass-card rounded-2xl p-5 border ${card.border} flex flex-col gap-3">
                <div class="flex items-center justify-between">
                    <span class="text-[10px] font-bold uppercase tracking-widest text-gray-500">${card.label}</span>
                    <div class="w-7 h-7 rounded-lg ${card.bg} flex items-center justify-center">
                        <svg class="w-4 h-4 ${card.color}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            ${card.icon}
                        </svg>
                    </div>
                </div>
                <div>
                    <span class="text-3xl font-bold ${card.color}">${card.value}</span>
                    <p class="text-[10px] text-gray-500 mt-1">${card.sub}</p>
                </div>
            </div>
        `).join('');
    }

    renderCards();

    // Projects
    unsubscribeOverview.push(
        onSnapshot(collection(db, 'projects'), snap => {
            counts.projects = snap.size;
            renderCards();
        }, () => { counts.projects = 'err'; renderCards(); })
    );

    // Customers
    unsubscribeOverview.push(
        onSnapshot(collection(db, 'customers'), snap => {
            counts.customers = snap.size;
            renderCards();
        }, () => { counts.customers = 'err'; renderCards(); })
    );

    // Devices — total + online
    unsubscribeOverview.push(
        onSnapshot(collection(db, 'devices'), snap => {
            counts.devices = snap.size;
            counts.devicesOnline = snap.docs.filter(d => d.data().status === 'online').length;
            renderCards();
        }, () => { counts.devices = 'err'; counts.devicesOnline = 'err'; renderCards(); })
    );

    // Recent alerts — last 50 mismatch/alert events
    unsubscribeOverview.push(
        onSnapshot(
            query(collection(db, 'sync_events'), where('event_type', 'in', ['mismatch', 'alert']), limit(50)),
            snap => { counts.alerts = snap.size; renderCards(); },
            () => { counts.alerts = 'err'; renderCards(); }
        )
    );
}

function initStatusPanel() {
    if (unsubscribeStatus) unsubscribeStatus();
    if (statusAgeInterval) { clearInterval(statusAgeInterval); statusAgeInterval = null; }

    if (!ACTIVE_DEVICE_ID) return;

    const statusDocRef = doc(db, "system_status", ACTIVE_DEVICE_ID);
    unsubscribeStatus = onSnapshot(statusDocRef, (docSnap) => {
        if (!docSnap.exists()) return;
        lastStatusUpdate = new Date();
        renderStatusCard(docSnap.data());
    }, (err) => console.error("Status panel error:", err));

    statusAgeInterval = setInterval(() => {
        if (!lastStatusUpdate) return;
        const sec = Math.round((Date.now() - lastStatusUpdate.getTime()) / 1000);
        statusAge.textContent = `Updated ${sec}s ago`;
    }, 1000);
}

// ── System Control Panel ───────────────────────────────────────────────────

const toggleInference = document.getElementById('toggle-inference');
const toggleCamera = document.getElementById('toggle-camera');
const toggleDisplay = document.getElementById('toggle-display');

// Initialize with defaults in case document doesn't exist
let currentControls = {
    inference_running: true,
    camera_active: true,
    display_active: true
};

async function sendControlsUpdate() {
    if (!ACTIVE_DEVICE_ID) return;
    try {
        await setDoc(doc(db, "device_control", ACTIVE_DEVICE_ID), {
            ...currentControls,
            ts: serverTimestamp()
        }, { merge: true });
    } catch (e) {
        console.error("Failed to update controls:", e);
    }
}

function handleToggle(key, element) {
    element.addEventListener('change', (e) => {
        currentControls[key] = e.target.checked;
        sendControlsUpdate();
    });
}

handleToggle('inference_running', toggleInference);
handleToggle('camera_active', toggleCamera);
handleToggle('display_active', toggleDisplay);

function initControlPanel() {
    if (unsubscribeControl) unsubscribeControl();
    if (!ACTIVE_DEVICE_ID) {
        toggleInference.disabled = true;
        toggleCamera.disabled = true;
        toggleDisplay.disabled = true;
        return;
    }
    unsubscribeControl = onSnapshot(doc(db, "device_control", ACTIVE_DEVICE_ID), (docSnap) => {
        // Enable toggles once we connect to Firebase
        toggleInference.disabled = false;
        toggleCamera.disabled = false;
        toggleDisplay.disabled = false;

        if (docSnap.exists()) {
            const data = docSnap.data();
            // Handle legacy command string OR new boolean flags
            if (data.command !== undefined && data.inference_running === undefined) {
                currentControls.inference_running = data.command === 'start';
            } else {
                currentControls = {
                    inference_running: data.inference_running !== false, // default true
                    camera_active: data.camera_active !== false,         // default true
                    display_active: data.display_active !== false        // default true
                };
            }
        }

        // Update UI
        toggleInference.checked = currentControls.inference_running;
        toggleCamera.checked = currentControls.camera_active;
        toggleDisplay.checked = currentControls.display_active;

    }, (err) => console.error("Control panel listener error:", err));
}

// ── Shared action button helper ───────────────────────────────────────────────

function actionBtns(editCls, deleteCls, deleteDataAttrs = '') {
    return `<div class="flex items-center gap-1.5 justify-end opacity-0 group-hover:opacity-100 transition-opacity">
        <button class="${editCls} edit-btn px-2.5 py-1 rounded-lg bg-white/5 text-gray-300 text-[10px] font-medium border border-white/10 hover:bg-white/10 transition-colors">Edit</button>
        <button class="${deleteCls} delete-btn px-2.5 py-1 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-medium border border-red-500/20 hover:bg-red-500/20 transition-colors" ${deleteDataAttrs}>Delete</button>
    </div>`;
}

// ── App Types ─────────────────────────────────────────────────────────────────

const appTypesTableBody = document.getElementById('app-types-table-body');
const addAppTypeBtn = document.getElementById('add-app-type-btn');
const addAppTypeModal = document.getElementById('add-app-type-modal');
const closeAppTypeModal = document.getElementById('close-app-type-modal');
const addAppTypeForm = document.getElementById('add-app-type-form');

let unsubscribeAppTypes = null;
let unsubscribeUsers = null;
let appTypesData = [];
let editingAppTypeId = null;

function openAppTypeModal(appType = null) {
    editingAppTypeId = appType ? appType.id : null;
    document.getElementById('app-type-modal-title').textContent = appType ? 'Edit App Type' : 'Create App Type';
    document.getElementById('app-type-name').value = appType?.name || '';
    document.getElementById('app-type-app-id').value = appType?.app_id || '';
    document.getElementById('app-type-eval-mode').value = appType?.evaluation_mode || '';
    if (evalModeHint) evalModeHint.textContent = appType?.evaluation_mode ? (EVAL_MODE_META[appType.evaluation_mode]?.hint || '') : '';
    document.getElementById('app-type-description').value = appType?.description || '';
    addAppTypeModal.classList.remove('hidden');
}

const EVAL_MODE_META = {
    exact_count:       { color: 'text-[#0a84ff] border-blue-500/20 bg-blue-500/15',   hint: 'Pass when detected count equals target exactly for every class.' },
    minimum_count:     { color: 'text-green-400 border-green-500/20 bg-green-500/15', hint: 'Pass when detected count ≥ minimum for every class.' },
    range_count:       { color: 'text-amber-400 border-amber-500/20 bg-amber-500/15', hint: 'Pass when min ≤ detected ≤ max per class.' },
    presence_check:    { color: 'text-purple-400 border-purple-500/20 bg-purple-500/15', hint: 'Pass when each required class appears at least once.' },
    packing_assurance: { color: 'text-rose-400 border-rose-500/20 bg-rose-500/15',    hint: 'Pass when each class meets its own individually configured rule.' },
};

const DOMAIN_BADGE = {
    surgical:  'bg-blue-500/15 text-blue-400 border-blue-500/20',
    od:        'bg-purple-500/15 text-purple-400 border-purple-500/20',
    inventory: 'bg-amber-500/15 text-amber-400 border-amber-500/20',
};

function initAppTypes() {
    if (unsubscribeAppTypes) unsubscribeAppTypes();
    const q = query(collection(db, 'app_types'), orderBy('created_at', 'desc'));
    unsubscribeAppTypes = onSnapshot(q, snap => {
        appTypesData = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        renderAppTypes();
        updateAppTypeSelect();
    }, err => console.error('App types error:', err));
}

function renderAppTypes() {
    if (!appTypesTableBody) return;
    if (appTypesData.length === 0) {
        appTypesTableBody.innerHTML = `
            <tr><td colspan="4" class="py-16 text-center">
                <div class="flex flex-col items-center gap-3 text-gray-500">
                    <svg class="w-10 h-10 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                            d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/>
                    </svg>
                    <span class="text-sm font-medium">No app types yet</span>
                    <button onclick="document.getElementById('add-app-type-btn').click()"
                        class="mt-1 px-4 py-1.5 bg-teal-500/10 text-teal-400 text-xs font-medium rounded-lg border border-teal-500/20 hover:bg-teal-500/20 transition-colors">
                        Create first app type
                    </button>
                </div>
            </td></tr>`;
        return;
    }

    appTypesTableBody.innerHTML = `
        <tr class="border-b border-white/5">
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Name</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Domain</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Evaluation Mode</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Description</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right"></th>
        </tr>
        ${appTypesData.map(t => {
            const meta = EVAL_MODE_META[t.evaluation_mode] || { color: 'text-gray-400 border-gray-500/20 bg-gray-500/15', hint: '' };
            const domainBadge = DOMAIN_BADGE[t.app_id] || DOMAIN_BADGE.od;
            return `
            <tr class="group hover:bg-white/[0.03] transition-all">
                <td class="py-3 pr-4 font-medium text-white text-sm">${t.name}</td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${domainBadge}">${t.app_id}</span>
                </td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${meta.color} font-mono text-[9px]">${t.evaluation_mode}</span>
                </td>
                <td class="py-3 pr-4 text-xs text-gray-500 max-w-[200px] truncate">${t.description || '—'}</td>
                <td class="py-3 text-right">
                    <div class="flex items-center gap-1.5 justify-end opacity-0 group-hover:opacity-100 transition-opacity">
                        <button class="edit-app-type-btn px-2.5 py-1 rounded-lg bg-white/5 text-gray-300 text-[10px] font-medium border border-white/10 hover:bg-white/10 transition-colors" data-id="${t.id}">Edit</button>
                        <button class="delete-app-type-btn px-2.5 py-1 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-medium border border-red-500/20 hover:bg-red-500/20 transition-colors" data-id="${t.id}" data-name="${t.name}">Delete</button>
                    </div>
                </td>
            </tr>`;
        }).join('')}`;

    appTypesTableBody.querySelectorAll('.edit-app-type-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const t = appTypesData.find(t => t.id === btn.dataset.id);
            if (t) openAppTypeModal(t);
        });
    });
    appTypesTableBody.querySelectorAll('.delete-app-type-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm(`Delete app type "${btn.dataset.name}"?`)) return;
            try { await deleteDoc(doc(db, 'app_types', btn.dataset.id)); }
            catch (err) { alert('Failed to delete: ' + err.message); }
        });
    });
}

function updateAppTypeSelect() {
    const sel = document.getElementById('app-app-type-id');
    if (!sel) return;
    sel.innerHTML = '<option value="">None</option>' +
        appTypesData.map(t => `<option value="${t.id}">${t.name} (${t.evaluation_mode})</option>`).join('');
}

// Evaluation mode hint on select change
const evalModeSelect = document.getElementById('app-type-eval-mode');
const evalModeHint = document.getElementById('eval-mode-hint');
if (evalModeSelect && evalModeHint) {
    evalModeSelect.addEventListener('change', () => {
        const meta = EVAL_MODE_META[evalModeSelect.value];
        evalModeHint.textContent = meta ? meta.hint : '';
    });
}

// Modal wiring
if (addAppTypeBtn) addAppTypeBtn.addEventListener('click', () => openAppTypeModal());
if (closeAppTypeModal) closeAppTypeModal.addEventListener('click', () => { addAppTypeModal.classList.add('hidden'); editingAppTypeId = null; });

// Create/Edit app type form
if (addAppTypeForm) addAppTypeForm.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    const fields = {
        name: document.getElementById('app-type-name').value.trim(),
        app_id: document.getElementById('app-type-app-id').value,
        evaluation_mode: document.getElementById('app-type-eval-mode').value,
        description: document.getElementById('app-type-description').value.trim(),
    };
    try {
        if (editingAppTypeId) {
            await updateDoc(doc(db, 'app_types', editingAppTypeId), { ...fields, updated_at: serverTimestamp() });
        } else {
            await addDoc(collection(db, 'app_types'), { ...fields, created_at: serverTimestamp() });
        }
        editingAppTypeId = null;
        e.target.reset();
        if (evalModeHint) evalModeHint.textContent = '';
        addAppTypeModal.classList.add('hidden');
    } catch (err) {
        console.error('Save app type error:', err);
        alert('Failed to save app type: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = ogText;
    }
});

// ── Applications ──────────────────────────────────────────────────────────────

const applicationsTableBody = document.getElementById('applications-table-body');
const addApplicationBtn = document.getElementById('add-application-btn');
const addApplicationModal = document.getElementById('add-application-modal');
const closeApplicationModal = document.getElementById('close-application-modal');
const addApplicationForm = document.getElementById('add-application-form');

let unsubscribeApplications = null;
let applicationsData = [];
let editingApplicationId = null;

function openApplicationModal(application = null) {
    editingApplicationId = application ? application.id : null;
    document.getElementById('application-modal-title').textContent = application ? 'Edit Application' : 'Create Application';
    updateApplicationSelects(application);
    document.getElementById('app-name').value = application?.name || '';
    document.getElementById('app-description').value = application?.description || '';
    document.getElementById('app-remarks').value = application?.remarks || '';
    addApplicationModal.classList.remove('hidden');
}

const APP_TYPE_BADGE = {
    surgical:  'bg-blue-500/15 text-blue-400 border-blue-500/20',
    od:        'bg-purple-500/15 text-purple-400 border-purple-500/20',
    inventory: 'bg-amber-500/15 text-amber-400 border-amber-500/20',
};

function initApplications() {
    if (unsubscribeApplications) unsubscribeApplications();
    const q = query(collection(db, 'applications'), orderBy('created_at', 'desc'));
    unsubscribeApplications = onSnapshot(q, snap => {
        applicationsData = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        renderApplications();
    }, err => console.error('Applications error:', err));
}

function renderApplications() {
    if (!applicationsTableBody) return;
    if (applicationsData.length === 0) {
        applicationsTableBody.innerHTML = `
            <tr><td colspan="6" class="py-16 text-center">
                <div class="flex flex-col items-center gap-3 text-gray-500">
                    <svg class="w-10 h-10 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                            d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/>
                    </svg>
                    <span class="text-sm font-medium">No applications yet</span>
                    <button onclick="document.getElementById('add-application-btn').click()"
                        class="mt-1 px-4 py-1.5 bg-amber-500/10 text-amber-400 text-xs font-medium rounded-lg border border-amber-500/20 hover:bg-amber-500/20 transition-colors">
                        Create first application
                    </button>
                </div>
            </td></tr>`;
        return;
    }

    applicationsTableBody.innerHTML = `
        <tr class="border-b border-white/5">
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Name</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Model</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Description</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Created</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right"></th>
        </tr>
        ${applicationsData.map(a => {
            const model = modelsData.find(m => m.id === a.model_id);
            const modelName = model
                ? `<span class="font-mono text-[10px]">${model.name} v${model.version}</span>`
                : '<span class="italic text-gray-600">—</span>';
            const created = a.created_at?.toDate
                ? a.created_at.toDate().toLocaleDateString()
                : '—';
            return `
            <tr class="group hover:bg-white/[0.03] transition-all">
                <td class="py-3 pr-4">
                    <span class="font-medium text-white text-sm">${a.name}</span>
                    ${a.remarks ? `<p class="text-[10px] text-gray-500 mt-0.5 truncate max-w-[160px]">${a.remarks}</p>` : ''}
                </td>
                <td class="py-3 pr-4 text-xs text-gray-400">${modelName}</td>
                <td class="py-3 pr-4 text-xs text-gray-500 max-w-[160px] truncate">${a.description || '<span class="italic text-gray-600">—</span>'}</td>
                <td class="py-3 pr-4 text-xs text-gray-500 font-mono">${created}</td>
                <td class="py-3 text-right">
                    <div class="flex items-center gap-1 justify-end">
                        <button class="edit-application-btn w-7 h-7 flex items-center justify-center rounded-lg bg-white/5 text-gray-400 border border-white/10 hover:bg-white/10 hover:text-white transition-colors" data-id="${a.id}" title="Edit">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 013.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"/></svg>
                        </button>
                        <button class="delete-application-btn w-7 h-7 flex items-center justify-center rounded-lg bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 transition-colors" data-id="${a.id}" data-name="${a.name}" title="Delete">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                        </button>
                    </div>
                </td>
            </tr>`;
        }).join('')}`;

    applicationsTableBody.querySelectorAll('.edit-application-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const application = applicationsData.find(a => a.id === btn.dataset.id);
            if (application) openApplicationModal(application);
        });
    });
    applicationsTableBody.querySelectorAll('.delete-application-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            alert(`Application "${btn.dataset.name}" cannot be deleted.\n\nApplications may be linked to active projects. Unlink from the project first, then archive if no longer needed.`);
        });
    });
}

function updateApplicationSelects(application = null) {
    const modelSel = document.getElementById('app-model-id');
    if (modelSel) {
        modelSel.innerHTML = '<option value="">None</option>' +
            modelsData.map(m => `<option value="${m.id}">${m.name} v${m.version}</option>`).join('');
        modelSel.value = application?.model_id || '';
    }
}

// Modal wiring
if (addApplicationBtn) addApplicationBtn.addEventListener('click', () => openApplicationModal());
if (closeApplicationModal) closeApplicationModal.addEventListener('click', () => { addApplicationModal.classList.add('hidden'); editingApplicationId = null; });

// Create/Edit application form
if (addApplicationForm) addApplicationForm.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    const fields = {
        name: document.getElementById('app-name').value.trim(),
        description: document.getElementById('app-description').value.trim(),
        model_id: document.getElementById('app-model-id').value || null,
        remarks: document.getElementById('app-remarks').value.trim(),
        updated_at: serverTimestamp(),
    };
    try {
        if (editingApplicationId) {
            await updateDoc(doc(db, 'applications', editingApplicationId), fields);
        } else {
            await addDoc(collection(db, 'applications'), { ...fields, created_at: serverTimestamp() });
        }
        editingApplicationId = null;
        e.target.reset();
        addApplicationModal.classList.add('hidden');
    } catch (err) {
        console.error('Save application error:', err);
        alert('Failed to save application: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = ogText;
    }
});

// ── Models ────────────────────────────────────────────────────────────────────

const modelsTableBody = document.getElementById('models-table-body');
const addModelBtn = document.getElementById('add-model-btn');
const addModelModal = document.getElementById('add-model-modal');
const closeModelModal = document.getElementById('close-model-modal');
const addModelForm = document.getElementById('add-model-form');

let unsubscribeModels = null;
let modelsData = [];
let editingModelId = null;

function openModelModal(model = null) {
    editingModelId = model ? model.id : null;
    document.getElementById('model-modal-title').textContent = model ? 'Edit Model' : 'Register Model';
    document.getElementById('model-name').value = model?.name || '';
    document.getElementById('model-version').value = model?.version || '';
    document.getElementById('model-type').value = model?.type || 'internal';
    document.getElementById('model-framework').value = model?.framework || 'yolov8';
    document.getElementById('model-input-resolution').value = model?.input_resolution || '';
    document.getElementById('model-class-count').value = model?.class_count || '';
    document.getElementById('model-hef-path').value = model?.hef_path || '';
    document.getElementById('model-class-labels').value = model?.class_labels?.join(', ') || '';
    document.getElementById('model-status').value = model?.status || 'active';
    addModelModal.classList.remove('hidden');
}

const MODEL_TYPE_BADGE = {
    internal:  'bg-blue-500/15 text-blue-400 border-blue-500/20',
    '3rd_party':'bg-purple-500/15 text-purple-400 border-purple-500/20',
};
const MODEL_STATUS_BADGE = {
    active:     'bg-green-500/15 text-green-400 border-green-500/20',
    deprecated: 'bg-gray-500/15 text-gray-400 border-gray-500/20',
};
const CONVERSION_STATUS_BADGE = {
    uploading:          'bg-gray-500/15 text-gray-400 border-gray-500/20',
    pending_conversion: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/20',
    processing:         'bg-blue-500/15 text-blue-400 border-blue-500/20',
    ready:              'bg-green-500/15 text-green-400 border-green-500/20',
    failed:             'bg-red-500/15 text-red-400 border-red-500/20',
};

// Estimate conversion progress (0–100) from log lines
function conversionProgress(log, status) {
    if (status === 'ready') return 100;
    if (status === 'failed') return 100;
    if (!Array.isArray(log) || log.length === 0) return 0;
    const joined = log.join('\n');
    if (/Uploading HEF/i.test(joined))   return 92;
    if (/hailo_compile/i.test(joined))   return 80;
    if (/hailo_optimize/i.test(joined))  return 62;
    if (/hailo_parse/i.test(joined))     return 46;
    if (/export_onnx/i.test(joined))     return 30;
    if (/inspect_model/i.test(joined))   return 18;
    if (/Downloading/i.test(joined))     return 8;
    if (/Conversion started/i.test(joined)) return 4;
    return 2;
}

function initModels() {
    if (unsubscribeModels) unsubscribeModels();
    const q = query(collection(db, 'models'), orderBy('created_at', 'desc'));
    unsubscribeModels = onSnapshot(q, snap => {
        modelsData = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        renderModels();
    }, err => console.error('Models error:', err));
}

function renderModels() {
    if (!modelsTableBody) return;
    if (modelsData.length === 0) {
        modelsTableBody.innerHTML = `
            <tr><td colspan="6" class="py-16 text-center">
                <div class="flex flex-col items-center gap-3 text-gray-500">
                    <svg class="w-10 h-10 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                            d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
                    </svg>
                    <span class="text-sm font-medium">No models registered yet</span>
                    <p class="text-xs text-gray-600 text-center max-w-xs">Run <code class="text-gray-500 bg-white/5 px-1.5 py-0.5 rounded font-mono">scripts/seed_models.py</code> to auto-import from the models/ directory, or register manually.</p>
                    <button onclick="document.getElementById('add-model-btn').click()"
                        class="mt-1 px-4 py-1.5 bg-purple-500/10 text-purple-400 text-xs font-medium rounded-lg border border-purple-500/20 hover:bg-purple-500/20 transition-colors">
                        Register first model
                    </button>
                </div>
            </td></tr>`;
        return;
    }

    modelsTableBody.innerHTML = `
        <tr class="border-b border-white/5">
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Name</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Framework</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Resolution</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Classes</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Status</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">HEF</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right"></th>
        </tr>
        ${modelsData.map(m => {
            const typeBadge = MODEL_TYPE_BADGE[m.type] || MODEL_TYPE_BADGE.internal;
            const statusBadge = MODEL_STATUS_BADGE[m.status] || MODEL_STATUS_BADGE.active;
            const classCount = Array.isArray(m.class_labels) ? m.class_labels.length : (m.class_count ?? '—');
            const convStatus = m.conversion_status;
            const convBadge = convStatus ? CONVERSION_STATUS_BADGE[convStatus] || CONVERSION_STATUS_BADGE.pending_conversion : null;
            const convLabel = convStatus ? convStatus.replace(/_/g, ' ') : null;
            const hasLog = Array.isArray(m.conversion_log) && m.conversion_log.length > 0;
            const logId = `conv-log-${m.id}`;
            const pct = convStatus ? conversionProgress(m.conversion_log, convStatus) : 0;
            const isActive = convStatus === 'uploading' || convStatus === 'pending_conversion' || convStatus === 'processing';
            const barColor = convStatus === 'ready' ? '#34c759'
                           : convStatus === 'failed' ? '#ff3b30'
                           : convStatus === 'processing' ? '#0a84ff'
                           : convStatus === 'pending_conversion' ? '#ff9f0a'
                           : '#8e8e93';
            return `
            <tr class="group hover:bg-white/[0.03] transition-all">
                <td class="py-3 pr-4">
                    <span class="font-medium text-white text-sm">${m.name}</span>
                    <div class="flex items-center gap-1.5 mt-1">
                        <span class="status-badge border ${typeBadge} text-[9px]">${m.type}</span>
                        <span class="text-[10px] text-gray-600 font-mono">v${m.version}</span>
                    </div>
                </td>
                <td class="py-3 pr-4 text-xs font-mono text-gray-400">${m.framework || '—'}</td>
                <td class="py-3 pr-4 text-xs font-mono text-gray-400">${m.input_resolution ? m.input_resolution + 'px' : '—'}</td>
                <td class="py-3 pr-4 text-xs text-gray-400">${classCount}</td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${statusBadge}">${m.status || 'active'}</span>
                </td>
                <td class="py-3 pr-4">
                    ${convBadge ? `
                    <div class="flex flex-col gap-1.5" style="min-width:90px">
                        <div class="flex items-center justify-between gap-2">
                            <span class="status-badge border ${convBadge} text-[9px]">${convLabel}</span>
                            ${isActive ? `<span class="text-[9px] text-gray-500 font-mono">${pct}%</span>` : ''}
                        </div>
                        <div class="conv-progress-track">
                            <div class="conv-progress-fill ${isActive && pct < 8 ? 'indeterminate' : ''}"
                                 style="width:${pct}%; background:${barColor}"></div>
                        </div>
                        ${convStatus === 'ready' && m.hef_download_url ? `
                        <a href="${escapeHtml(m.hef_download_url)}" target="_blank" class="text-[9px] text-indigo-400 hover:text-indigo-300 underline underline-offset-2">Download HEF</a>
                        ` : ''}
                        ${convStatus === 'failed' ? `
                        <button class="retry-conv-btn text-[9px] text-yellow-500 hover:text-yellow-300 text-left" data-id="${m.id}">Retry</button>
                        ` : ''}
                        ${hasLog ? `
                        <button class="toggle-log-btn text-[9px] text-gray-500 hover:text-gray-300 text-left" data-log-id="${logId}">View log</button>
                        ` : ''}
                    </div>
                    ` : '<span class="text-gray-700 text-xs">—</span>'}
                </td>
                <td class="py-3 text-right">
                    <div class="flex items-center gap-1 justify-end">
                        <button class="edit-model-btn w-7 h-7 flex items-center justify-center rounded-lg bg-white/5 text-gray-400 border border-white/10 hover:bg-white/10 hover:text-white transition-colors" data-id="${m.id}" title="Edit">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 013.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"/></svg>
                        </button>
                        <button class="delete-model-btn w-7 h-7 flex items-center justify-center rounded-lg bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 transition-colors" data-id="${m.id}" data-name="${m.name}" title="Delete">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                        </button>
                    </div>
                </td>
            </tr>
            ${hasLog ? `
            <tr id="${logId}" class="hidden">
                <td colspan="7" class="pb-3 pr-4">
                    <div class="bg-black/40 border border-white/5 rounded-xl px-3 py-2 max-h-40 overflow-y-auto custom-scrollbar">
                        <pre class="text-[10px] text-gray-400 font-mono whitespace-pre-wrap">${(m.conversion_log || []).join('\n')}</pre>
                    </div>
                </td>
            </tr>` : ''}`;
        }).join('')}`;

    modelsTableBody.querySelectorAll('.retry-conv-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            try {
                await updateDoc(doc(db, 'models', btn.dataset.id), {
                    conversion_status: 'pending_conversion',
                    error_message: null,
                });
            } catch (err) { alert('Failed to retry: ' + err.message); }
        });
    });

    modelsTableBody.querySelectorAll('.toggle-log-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const row = document.getElementById(btn.dataset.logId);
            if (row) {
                row.classList.toggle('hidden');
                btn.textContent = row.classList.contains('hidden') ? 'View log' : 'Hide log';
            }
        });
    });
    modelsTableBody.querySelectorAll('.edit-model-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const model = modelsData.find(m => m.id === btn.dataset.id);
            if (model) openModelModal(model);
        });
    });
    modelsTableBody.querySelectorAll('.delete-model-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            alert(`Model "${btn.dataset.name}" cannot be deleted.\n\nModels may be referenced by active projects. Remove the model from all projects first.`);
        });
    });
}

// ── Convert → HEF modal wiring ──────────────────────────────────────────────
const convertHefBtn = document.getElementById('convert-hef-btn');
const convertHefModal = document.getElementById('convert-hef-modal');
const closeConvertModal = document.getElementById('close-convert-modal');
const convertHefForm = document.getElementById('convert-hef-form');

if (convertHefBtn) convertHefBtn.addEventListener('click', () => {
    convertHefModal.classList.remove('hidden');
});
if (closeConvertModal) closeConvertModal.addEventListener('click', () => {
    convertHefModal.classList.add('hidden');
    convertHefForm.reset();
    document.getElementById('conv-upload-progress').classList.add('hidden');
    document.getElementById('conv-error-msg').classList.add('hidden');
});

if (convertHefForm) convertHefForm.addEventListener('submit', async e => {
    e.preventDefault();
    const submitBtn = document.getElementById('conv-submit-btn');
    const errorMsg = document.getElementById('conv-error-msg');
    const progressDiv = document.getElementById('conv-upload-progress');
    const progressBar = document.getElementById('conv-progress-bar');
    const progressPct = document.getElementById('conv-progress-pct');

    errorMsg.classList.add('hidden');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Uploading…';

    const modelName = document.getElementById('conv-model-name').value.trim();
    const format = document.getElementById('conv-format').value;
    const hwArch = document.getElementById('conv-hw-arch').value;
    const file = document.getElementById('conv-file').files[0];

    if (!file) {
        errorMsg.textContent = 'Please select a file.';
        errorMsg.classList.remove('hidden');
        submitBtn.disabled = false;
        submitBtn.textContent = 'Upload & Start Conversion';
        return;
    }

    try {
        // Create doc with status "uploading" — converter ignores this until upload finishes
        const modelRef = await addDoc(collection(db, 'models'), {
            name: modelName,
            version: '1.0.0',
            type: 'internal',
            framework: format === 'pt' ? 'yolov8' : 'custom',
            hw_arch: hwArch,
            original_format: format,
            conversion_status: 'uploading',
            conversion_log: [],
            status: 'active',
            created_at: serverTimestamp(),
        });
        const modelId = modelRef.id;
        const storagePath = `uploads/raw/${modelId}/original.${format}`;

        // Upload to Firebase Storage
        progressDiv.classList.remove('hidden');
        const storageRef = ref(storage, storagePath);
        const uploadTask = uploadBytesResumable(storageRef, file);

        uploadTask.on('state_changed',
            snapshot => {
                const pct = Math.round((snapshot.bytesTransferred / snapshot.totalBytes) * 100);
                progressBar.style.width = pct + '%';
                progressPct.textContent = pct + '%';
            },
            err => {
                errorMsg.textContent = 'Upload failed: ' + err.message;
                errorMsg.classList.remove('hidden');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Upload & Start Conversion';
            },
            async () => {
                // File is fully uploaded — now set pending_conversion so converter picks it up
                await updateDoc(doc(db, 'models', modelId), {
                    storage_raw_path: storagePath,
                    model_name: modelName,
                    conversion_status: 'pending_conversion',
                });
                convertHefModal.classList.add('hidden');
                convertHefForm.reset();
                progressDiv.classList.add('hidden');
                submitBtn.disabled = false;
                submitBtn.textContent = 'Upload & Start Conversion';
                document.querySelector('[data-tab="models"]').click();
            }
        );
    } catch (err) {
        errorMsg.textContent = 'Error: ' + err.message;
        errorMsg.classList.remove('hidden');
        submitBtn.disabled = false;
        submitBtn.textContent = 'Upload & Start Conversion';
    }
});

// Modal wiring
if (addModelBtn) addModelBtn.addEventListener('click', () => openModelModal());
if (closeModelModal) closeModelModal.addEventListener('click', () => { addModelModal.classList.add('hidden'); editingModelId = null; });

// Create/Edit model form
if (addModelForm) addModelForm.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    const labelsRaw = document.getElementById('model-class-labels').value;
    const classLabels = labelsRaw
        ? labelsRaw.split(',').map(l => l.trim()).filter(Boolean)
        : [];
    const fields = {
        name: document.getElementById('model-name').value.trim(),
        version: document.getElementById('model-version').value.trim(),
        type: document.getElementById('model-type').value,
        framework: document.getElementById('model-framework').value,
        input_resolution: parseInt(document.getElementById('model-input-resolution').value, 10),
        class_count: parseInt(document.getElementById('model-class-count').value, 10),
        class_labels: classLabels,
        hef_path: document.getElementById('model-hef-path').value.trim(),
        status: document.getElementById('model-status').value,
    };
    try {
        if (editingModelId) {
            await updateDoc(doc(db, 'models', editingModelId), { ...fields, updated_at: serverTimestamp() });
        } else {
            await addDoc(collection(db, 'models'), { ...fields, created_at: serverTimestamp() });
        }
        editingModelId = null;
        e.target.reset();
        addModelModal.classList.add('hidden');
    } catch (err) {
        console.error('Save model error:', err);
        alert('Failed to save model: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = ogText;
    }
});

// ── Projects ──────────────────────────────────────────────────────────────────

const projectsTableBody = document.getElementById('projects-table-body');
const addProjectBtn = document.getElementById('add-project-btn');
const addProjectModal = document.getElementById('add-project-modal');
const closeProjectModal = document.getElementById('close-project-modal');
const addProjectForm = document.getElementById('add-project-form');

let unsubscribeProjects = null;
let projectsData = [];
let editingProjectId = null;

function openProjectModal(project = null) {
    editingProjectId = project ? project.id : null;
    document.getElementById('project-modal-title').textContent = project ? 'Edit Project' : 'Create Project';
    updateProjectSelects(project);
    document.getElementById('project-name').value = project?.name || '';
    document.getElementById('project-description').value = project?.description || '';
    document.getElementById('project-app-id').value = project?.app_id || '';
    document.getElementById('project-status').value = project?.status || 'active';
    addProjectModal.classList.remove('hidden');
}

const APP_ID_BADGE = {
    surgical:        'bg-blue-500/15 text-blue-400 border-blue-500/20',
    od:              'bg-purple-500/15 text-purple-400 border-purple-500/20',
    inventory:       'bg-amber-500/15 text-amber-400 border-amber-500/20',
    inventory_count: 'bg-teal-500/15 text-teal-400 border-teal-500/20',
};
const STATUS_BADGE = {
    active:   'bg-green-500/15 text-green-400 border-green-500/20',
    paused:   'bg-amber-500/15 text-amber-400 border-amber-500/20',
    archived: 'bg-gray-500/15 text-gray-400 border-gray-500/20',
};

function initProjects() {
    if (unsubscribeProjects) unsubscribeProjects();
    const q = query(collection(db, 'projects'), orderBy('created_at', 'desc'));
    unsubscribeProjects = onSnapshot(q, snap => {
        projectsData = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        renderProjects();
        updateProjectCustomerSelect();
    }, err => console.error('Projects error:', err));
}

function renderProjects() {
    if (!projectsTableBody) return;
    if (projectsData.length === 0) {
        projectsTableBody.innerHTML = `
            <tr><td colspan="6" class="py-16 text-center">
                <div class="flex flex-col items-center gap-3 text-gray-500">
                    <svg class="w-10 h-10 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>
                    </svg>
                    <span class="text-sm font-medium">No projects yet</span>
                    <button onclick="document.getElementById('add-project-btn').click()"
                        class="mt-1 px-4 py-1.5 bg-[#0a84ff]/10 text-[#0a84ff] text-xs font-medium rounded-lg border border-[#0a84ff]/20 hover:bg-[#0a84ff]/20 transition-colors">
                        Create first project
                    </button>
                </div>
            </td></tr>`;
        return;
    }

    projectsTableBody.innerHTML = `
        <tr class="border-b border-white/5">
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Name</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Customer</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Type</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Status</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Created</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right"></th>
        </tr>
        ${projectsData.map(p => {
            const customer = customersData.find(c => c.id === p.customer_id);
            const customerName = customer ? customer.name : '<span class="italic text-gray-600">—</span>';
            const appBadge = APP_ID_BADGE[p.app_id] || 'bg-gray-500/15 text-gray-400 border-gray-500/20';
            const statusBadge = STATUS_BADGE[p.status] || STATUS_BADGE.active;
            const created = p.created_at?.toDate
                ? p.created_at.toDate().toLocaleDateString()
                : '—';
            return `
            <tr class="group hover:bg-white/[0.03] transition-all cursor-pointer" onclick="window.openProjectDetail('${p.id}')">
                <td class="py-3 pr-4">
                    <span class="font-medium text-white text-sm">${p.name}</span>
                    ${p.description ? `<p class="text-[10px] text-gray-500 mt-0.5 truncate max-w-[180px]">${p.description}</p>` : ''}
                </td>
                <td class="py-3 pr-4 text-xs text-gray-400">${customerName}</td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${appBadge}">${p.app_id || '—'}</span>
                </td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${statusBadge}">${p.status || 'active'}</span>
                </td>
                <td class="py-3 pr-4 text-xs text-gray-500 font-mono">${created}</td>
                <td class="py-3 text-right" onclick="event.stopPropagation()">
                    <div class="flex items-center gap-1 justify-end">
                        <button class="edit-project-btn w-7 h-7 flex items-center justify-center rounded-lg bg-white/5 text-gray-400 border border-white/10 hover:bg-white/10 hover:text-white transition-colors" data-id="${p.id}" title="Edit">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 013.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"/></svg>
                        </button>
                        <button class="delete-project-btn w-7 h-7 flex items-center justify-center rounded-lg bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 transition-colors" data-id="${p.id}" data-name="${p.name}" title="Delete">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                        </button>
                    </div>
                </td>
            </tr>`;
        }).join('')}`;

    projectsTableBody.querySelectorAll('.edit-project-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const project = projectsData.find(p => p.id === btn.dataset.id);
            if (project) openProjectModal(project);
        });
    });
    projectsTableBody.querySelectorAll('.delete-project-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm(`Delete project "${btn.dataset.name}"?\n\nThe project will be removed. Linked devices, application, and model references will be unlinked but not deleted.`)) return;
            try {
                const projectId = btn.dataset.id;
                const batch = writeBatch(db);

                // Delete the project document
                batch.delete(doc(db, 'projects', projectId));

                // Clear project_id on any linked applications
                applicationsData
                    .filter(a => a.project_id === projectId)
                    .forEach(a => batch.update(doc(db, 'applications', a.id), { project_id: null }));

                // Clear project_id on any linked devices and clean up device-specific collections
                const project = projectsData.find(p => p.id === projectId);
                const linkedDeviceIds = project?.device_ids || [];
                linkedDeviceIds.forEach(docId => {
                    batch.update(doc(db, 'devices', docId), {
                        project_id: null,
                        updated_at: serverTimestamp(),
                    });
                    // Cascade-clean client-writable device collections
                    const device = devicesData.find(d => d.id === docId);
                    if (device?.device_id) {
                        batch.delete(doc(db, 'device_control', device.device_id));
                        batch.delete(doc(db, 'job_config', device.device_id));
                        batch.delete(doc(db, 'gas_config', device.device_id));
                    }
                });

                await batch.commit();
            } catch (err) { alert('Failed to delete: ' + err.message); }
        });
    });
}

// ── Project Detail Modal ──────────────────────────────────────────────────────

const projectDetailModal = document.getElementById('project-detail-modal');
const projectDetailBackdrop = document.getElementById('project-detail-backdrop');
const closeProjectDetailBtn = document.getElementById('close-project-detail');

function _detailRow(label, value) {
    return `<div class="flex justify-between items-start gap-2">
        <span class="text-[10px] text-gray-500 shrink-0">${label}</span>
        <span class="text-xs text-gray-200 text-right">${value || '<span class="italic text-gray-600">—</span>'}</span>
    </div>`;
}

window.openProjectDetail = function(projectId) {
    const p = projectsData.find(x => x.id === projectId);
    if (!p || !projectDetailModal) return;

    // Header
    document.getElementById('pd-name').textContent = p.name || '—';
    document.getElementById('pd-desc').textContent = p.description || '';

    // Customer card
    const customer = customersData.find(c => c.id === p.customer_id);
    document.getElementById('pd-customer').innerHTML = customer
        ? _detailRow('Name', customer.name) +
          _detailRow('Contact', customer.contact_email || customer.email) +
          _detailRow('Country', customer.country)
        : '<span class="text-xs italic text-gray-600">No customer linked</span>';

    // Devices card
    const deviceIds = Array.isArray(p.device_ids) ? p.device_ids : [];
    const linkedDevices = devicesData.filter(d => deviceIds.includes(d.id));
    document.getElementById('pd-devices').innerHTML = linkedDevices.length
        ? linkedDevices.map(d =>
            `<div class="flex items-center gap-2">
                <span class="w-1.5 h-1.5 rounded-full bg-purple-400 shrink-0"></span>
                <span class="text-xs text-gray-200">${d.name || d.device_id || d.id}</span>
                ${d.type ? `<span class="text-[9px] text-gray-500">${d.type}</span>` : ''}
            </div>`).join('')
        : '<span class="text-xs italic text-gray-600">No devices linked</span>';

    // AI Model card — prefer project.model_id, fall back to application's model_id
    const app = applicationsData.find(a => a.project_id === p.id);
    const modelId = p.model_id || (app && app.model_id);
    const model = modelsData.find(m => m.id === modelId);
    document.getElementById('pd-model').innerHTML = model
        ? _detailRow('Name', model.name) +
          _detailRow('Version', model.version) +
          _detailRow('Framework', model.framework)
        : '<span class="text-xs italic text-gray-600">No model linked</span>';

    // Application card
    const appType = app && appTypesData.find(t => t.id === app.app_type_id);
    document.getElementById('pd-application').innerHTML = app
        ? _detailRow('Name', app.name) +
          _detailRow('Type', appType ? appType.name : app.app_type_id) +
          _detailRow('Status', app.status)
        : '<span class="text-xs italic text-gray-600">No application linked</span>';

    // Footer
    const statusBadge = STATUS_BADGE[p.status] || STATUS_BADGE.active;
    const appBadge = APP_ID_BADGE[p.app_id] || 'bg-gray-500/15 text-gray-400 border-gray-500/20';
    document.getElementById('pd-status-badge').className = `status-badge border ${statusBadge}`;
    document.getElementById('pd-status-badge').textContent = p.status || 'active';
    document.getElementById('pd-app-badge').className = `status-badge border ${appBadge}`;
    document.getElementById('pd-app-badge').textContent = p.app_id || '';
    document.getElementById('pd-created').textContent = p.created_at?.toDate
        ? 'Created ' + p.created_at.toDate().toLocaleDateString()
        : '';

    projectDetailModal.style.display = 'flex';
};

function closeProjectDetail() {
    if (projectDetailModal) projectDetailModal.style.display = 'none';
}

if (closeProjectDetailBtn) closeProjectDetailBtn.addEventListener('click', closeProjectDetail);

// ─────────────────────────────────────────────────────────────────────────────

function updateProjectSelects(project = null) {
    // Customer
    const customerSel = document.getElementById('project-customer-id');
    if (customerSel) {
        customerSel.innerHTML = '<option value="">None</option>' +
            customersData.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
        customerSel.value = project?.customer_id || '';
    }

    // Device (single-select, 1:1 binding)
    const deviceSel = document.getElementById('project-device-ids');
    if (deviceSel) {
        const selectedIds = Array.isArray(project?.device_ids) ? project.device_ids : [];
        const currentProjectId = project?.id || editingProjectId;
        deviceSel.innerHTML = '<option value="">None</option>' + devicesData.map(d => {
            const isSelected = selectedIds.includes(d.id);
            const ownerProject = projectsData.find(p => p.id !== currentProjectId && (p.device_ids || []).includes(d.id));
            const inUse = !isSelected && ownerProject;
            const label = d.name || d.device_id || d.id;
            const suffix = inUse ? ` (In use by: ${ownerProject.name || ownerProject.id})` : '';
            return `<option value="${d.id}" ${isSelected ? 'selected' : ''} ${inUse ? 'disabled' : ''}>${label}${suffix}</option>`;
        }).join('');
    }

    // AI Model
    const modelSel = document.getElementById('project-model-id');
    if (modelSel) {
        modelSel.innerHTML = '<option value="">None</option>' +
            modelsData.map(m => `<option value="${m.id}">${m.name} v${m.version || ''}</option>`).join('');
        modelSel.value = project?.model_id || '';
    }

    // Application
    const appSel = document.getElementById('project-application-id');
    if (appSel) {
        appSel.innerHTML = '<option value="">None</option>' +
            applicationsData.map(a => `<option value="${a.id}">${a.name}</option>`).join('');
        appSel.value = project?.application_id || '';
    }
}

function updateProjectCustomerSelect() { updateProjectSelects(); }

// Modal wiring
if (addProjectBtn) addProjectBtn.addEventListener('click', () => openProjectModal());
if (closeProjectModal) closeProjectModal.addEventListener('click', () => { addProjectModal.classList.add('hidden'); editingProjectId = null; });

// Create/Edit project form
if (addProjectForm) addProjectForm.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving…';
    const deviceSel = document.getElementById('project-device-ids');
    const selectedDeviceIds = deviceSel && deviceSel.value ? [deviceSel.value] : [];
    const fields = {
        name: document.getElementById('project-name').value.trim(),
        description: document.getElementById('project-description').value.trim(),
        app_id: document.getElementById('project-app-id').value,
        customer_id: document.getElementById('project-customer-id').value || null,
        device_ids: selectedDeviceIds,
        model_id: document.getElementById('project-model-id').value || null,
        application_id: document.getElementById('project-application-id').value || null,
        status: document.getElementById('project-status').value,
        updated_at: serverTimestamp(),
    };
    try {
        let projectId;
        let oldDeviceIds = [];

        if (editingProjectId) {
            // Read old device_ids before overwriting so we can detect removals
            const existing = await getDoc(doc(db, 'projects', editingProjectId));
            if (existing.exists()) {
                oldDeviceIds = existing.data().device_ids || [];
            }
            await updateDoc(doc(db, 'projects', editingProjectId), fields);
            projectId = editingProjectId;
        } else {
            const ref = await addDoc(collection(db, 'projects'), {
                ...fields,
                created_at: serverTimestamp(),
            });
            projectId = ref.id;
        }

        // Back-write project_id to newly linked devices + send reset signal
        const addedIds = selectedDeviceIds.filter(id => !oldDeviceIds.includes(id));
        for (const docId of addedIds) {
            try {
                await updateDoc(doc(db, 'devices', docId), {
                    project_id: projectId,
                    updated_at: serverTimestamp(),
                });
                const device = devicesData.find(d => d.id === docId);
                if (device?.device_id) {
                    const projectModel = modelsData.find(m => m.id === fields.model_id);
                    const hefModel = projectModel?.hef_path?.replace('/app/models/', '') || '';
                    const composeFile = fields.app_id === 'inventory_count'
                        ? 'docker-compose.gas.yml'
                        : 'docker-compose.yml';
                    await setDoc(doc(db, 'device_control', device.device_id), {
                        inference_running: true,
                        camera_active: true,
                        display_active: true,
                        reset: true,
                        project_id: projectId,
                        app_id: fields.app_id,
                        hef_model: hefModel,
                        compose_file: composeFile,
                        ts: serverTimestamp(),
                    });
                }
            } catch (linkErr) {
                console.warn('Device link update failed for', docId, linkErr);
            }
        }

        // Clear project_id from devices removed from this project
        const removedIds = oldDeviceIds.filter(id => !selectedDeviceIds.includes(id));
        for (const docId of removedIds) {
            try {
                await updateDoc(doc(db, 'devices', docId), {
                    project_id: null,
                    updated_at: serverTimestamp(),
                });
            } catch (unlinkErr) {
                console.warn('Device unlink update failed for', docId, unlinkErr);
            }
        }

        editingProjectId = null;
        e.target.reset();
        addProjectModal.classList.add('hidden');
    } catch (err) {
        console.error('Save project error:', err);
        alert('Failed to save project: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = ogText;
    }
});

// ── Customer & Device Management ──────────────────────────────────────────────

const customersTableBody = document.getElementById('customers-table-body');
const devicesTableBody = document.getElementById('devices-table-body');
const deviceCustomerSelect = document.getElementById('device-customer');

const addCustomerBtn = document.getElementById('add-customer-btn');
const addCustomerModal = document.getElementById('add-customer-modal');
const closeCustomerModal = document.getElementById('close-customer-modal');
const addCustomerForm = document.getElementById('add-customer-form');

const addDeviceBtn = document.getElementById('add-device-btn');
const addDeviceModal = document.getElementById('add-device-modal');
const closeDeviceModal = document.getElementById('close-device-modal');
const addDeviceForm = document.getElementById('add-device-form');

let customersData = [];
let editingCustomerId = null;

function openCustomerModal(customer = null) {
    editingCustomerId = customer ? customer.id : null;
    document.getElementById('customer-modal-title').textContent = customer ? 'Edit Customer' : 'Add Customer';
    document.getElementById('customer-name').value = customer?.name || '';
    document.getElementById('customer-contact-name').value = customer?.contact_name || '';
    document.getElementById('customer-phone').value = customer?.phone || '';
    document.getElementById('customer-contact').value = customer?.contact || '';
    document.getElementById('customer-industry').value = customer?.industry || 'hospital';
    document.getElementById('customer-country').value = customer?.country || '';
    document.getElementById('customer-address').value = customer?.address || '';
    document.getElementById('customer-notes').value = customer?.notes || '';
    addCustomerModal.classList.remove('hidden');
}

// Customer Listeners & Rendering
function initCustomers() {
    const q = query(collection(db, "customers"), orderBy("name", "asc"));
    unsubscribeCustomers = onSnapshot(q, (snapshot) => {
        customersData = [];
        snapshot.forEach((doc) => {
            customersData.push({ id: doc.id, ...doc.data() });
        });
        renderCustomers();
        updateDeviceCustomerSelect();
    }, (error) => console.error("Error fetching customers:", error));
}

const INDUSTRY_BADGE = {
    hospital:       'bg-blue-500/15 text-blue-400 border-blue-500/20',
    clinic:         'bg-cyan-500/15 text-cyan-400 border-cyan-500/20',
    pharma:         'bg-purple-500/15 text-purple-400 border-purple-500/20',
    medical_device: 'bg-teal-500/15 text-teal-400 border-teal-500/20',
    research:       'bg-amber-500/15 text-amber-400 border-amber-500/20',
    manufacturing:  'bg-orange-500/15 text-orange-400 border-orange-500/20',
    other:          'bg-gray-500/15 text-gray-400 border-gray-500/20',
};

const CUSTOMER_STATUS_BADGE = {
    pending: 'bg-amber-500/15 text-amber-400 border-amber-500/20',
    active:  'bg-green-500/15 text-green-400 border-green-500/20',
};

function renderCustomers() {
    if (customersData.length === 0) {
        customersTableBody.innerHTML = '<tr><td colspan="6" class="py-12 text-center text-appleMuted italic text-xs">No customers found.</td></tr>';
        return;
    }
    customersTableBody.innerHTML = `
        <tr class="border-b border-white/5">
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Organization</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Contact</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Industry</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Country</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-left pr-4">Status</th>
            <th class="pb-2 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right"></th>
        </tr>
        ${customersData.map(c => {
            const industryBadge = INDUSTRY_BADGE[c.industry] || INDUSTRY_BADGE.other;
            const industryLabel = c.industry ? c.industry.replace('_', ' ') : '—';
            const status = c.status || 'pending';
            const statusBadge = CUSTOMER_STATUS_BADGE[status] || CUSTOMER_STATUS_BADGE.pending;
            const approveBtn = status === 'pending'
                ? `<button class="approve-customer-btn px-2.5 py-1 rounded-lg bg-green-500/10 text-green-400 text-[10px] font-medium border border-green-500/20 hover:bg-green-500/20 transition-colors" data-id="${c.id}">Approve</button>`
                : '';
            return `
            <tr class="group hover:bg-white/[0.03] transition-all">
                <td class="py-3 pr-4">
                    <span class="font-medium text-white text-sm">${c.name}</span>
                    ${c.address ? `<p class="text-[10px] text-gray-600 mt-0.5 truncate max-w-[180px]">${c.address}</p>` : ''}
                </td>
                <td class="py-3 pr-4">
                    <span class="text-xs text-gray-300">${c.contact_name || ''}</span>
                    <p class="text-[10px] text-gray-500 mt-0.5">${c.contact}</p>
                    ${c.phone ? `<p class="text-[10px] text-gray-600 font-mono">${c.phone}</p>` : ''}
                </td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${industryBadge} text-[9px]">${industryLabel}</span>
                </td>
                <td class="py-3 pr-4 text-xs text-gray-500">${c.country || '—'}</td>
                <td class="py-3 pr-4">
                    <span class="status-badge border ${statusBadge} text-[9px]">${status}</span>
                </td>
                <td class="py-3 text-right">
                    <div class="flex items-center gap-1.5 justify-end opacity-0 group-hover:opacity-100 transition-opacity">
                        ${approveBtn}
                        <button class="edit-customer-btn px-2.5 py-1 rounded-lg bg-white/5 text-gray-300 text-[10px] font-medium border border-white/10 hover:bg-white/10 transition-colors" data-id="${c.id}">Edit</button>
                        <button class="delete-customer-btn px-2.5 py-1 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-medium border border-red-500/20 hover:bg-red-500/20 transition-colors" data-id="${c.id}" data-name="${c.name}">Delete</button>
                    </div>
                </td>
            </tr>`;
        }).join('')}`;

    customersTableBody.querySelectorAll('.approve-customer-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            try {
                await updateDoc(doc(db, 'customers', btn.dataset.id), {
                    status: 'active',
                    approved_at: serverTimestamp(),
                });
            } catch (err) {
                console.error('Approve customer error:', err);
                alert('Failed to approve: ' + err.message);
            }
        });
    });
    customersTableBody.querySelectorAll('.delete-customer-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            alert(`Customer "${btn.dataset.name}" cannot be deleted.\n\nCustomers may be linked to active projects. Remove the customer from all projects first.`);
        });
    });
    customersTableBody.querySelectorAll('.edit-customer-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const customer = customersData.find(c => c.id === btn.dataset.id);
            if (customer) openCustomerModal(customer);
        });
    });
}

function updateDeviceCustomerSelect() {
    deviceCustomerSelect.innerHTML = '<option value="" disabled selected>Select Customer</option>' +
        customersData.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
}

// Device Listeners & Rendering
let devicesData = [];
let editingDeviceId = null;

function openDeviceModal(device = null) {
    editingDeviceId = device ? device.id : null;
    document.getElementById('device-modal-title').textContent = device ? 'Edit Device' : 'Register Device';
    const deviceIdInput = document.getElementById('device-id-input');
    deviceIdInput.value = device?.device_id || '';
    deviceIdInput.readOnly = !!device;
    deviceIdInput.classList.toggle('opacity-50', !!device);
    updateDeviceCustomerSelect();
    document.getElementById('device-customer').value = device?.customer_id || '';
    document.getElementById('device-location').value = device?.location || '';
    addDeviceModal.classList.remove('hidden');
}

function initDevices() {
    const q = query(collection(db, "devices"), orderBy("device_id", "asc"));
    unsubscribeDevices = onSnapshot(q, (snapshot) => {
        devicesData = [];
        snapshot.forEach((doc) => {
            devicesData.push({ id: doc.id, ...doc.data() });
        });
        renderDevices(devicesData);
    }, (error) => console.error("Error fetching devices:", error));
}

function renderDevices(devicesData) {
    if (devicesData.length === 0) {
        devicesTableBody.innerHTML = '<tr><td colspan="4" class="py-4 text-center text-appleMuted italic text-xs">No devices found.</td></tr>';
        return;
    }
    devicesTableBody.innerHTML = devicesData.map(d => {
        const customer = customersData.find(c => c.id === d.customer_id);
        const customerName = customer ? customer.name : `<span class="italic text-gray-500">Unknown (${d.customer_id})</span>`;
        const isActive = d.device_id === ACTIVE_DEVICE_ID;
        const statusDot = d.status === 'online'
            ? '<span class="inline-block w-2 h-2 rounded-full bg-green-400 mr-1"></span>'
            : '<span class="inline-block w-2 h-2 rounded-full bg-gray-500 mr-1"></span>';
        return `
        <tr class="group hover:bg-white/[0.03] transition-all cursor-pointer ${isActive ? 'bg-white/[0.06]' : ''}"
            data-device-id="${d.device_id}" onclick="selectDevice('${d.device_id}')">
            <td class="py-3 font-mono font-medium text-xs ${isActive ? 'text-green-400' : 'text-white'}">${statusDot}${d.device_id}</td>
            <td class="py-3 text-appleMuted text-xs">${customerName}</td>
            <td class="py-3 text-gray-400 text-xs">${d.app_id || ''}</td>
            <td class="py-3 text-right" onclick="event.stopPropagation()">
                <div class="flex items-center gap-1.5 justify-end opacity-0 group-hover:opacity-100 transition-opacity">
                    <button class="edit-device-btn px-2.5 py-1 rounded-lg bg-white/5 text-gray-300 text-[10px] font-medium border border-white/10 hover:bg-white/10 transition-colors" data-id="${d.id}">Edit</button>
                    <button class="delete-device-btn px-2.5 py-1 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-medium border border-red-500/20 hover:bg-red-500/20 transition-colors" data-id="${d.id}" data-name="${d.device_id}">Delete</button>
                </div>
            </td>
        </tr>
    `;
    }).join('');

    devicesTableBody.querySelectorAll('.edit-device-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const device = devicesData.find(d => d.id === btn.dataset.id);
            if (device) openDeviceModal(device);
        });
    });
    devicesTableBody.querySelectorAll('.delete-device-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            alert(`Device "${btn.dataset.name}" cannot be deleted.\n\nDevices may be assigned to active projects. Unassign from the project first, then decommission.`);
        });
    });
}

window.selectDevice = function(deviceId) {
    ACTIVE_DEVICE_ID = deviceId;
    // Restart device-scoped listeners
    initStatusPanel();
    initControlPanel();
    // Re-render to update active highlight
    const rows = devicesTableBody.querySelectorAll('tr[data-device-id]');
    rows.forEach(row => {
        const isActive = row.dataset.deviceId === ACTIVE_DEVICE_ID;
        row.classList.toggle('bg-white/[0.06]', isActive);
        const idCell = row.querySelector('td:first-child');
        if (idCell) {
            idCell.classList.toggle('text-green-400', isActive);
            idCell.classList.toggle('text-white', !isActive);
        }
    });
};

// Modals
addCustomerBtn.addEventListener('click', () => openCustomerModal());
closeCustomerModal.addEventListener('click', () => { addCustomerModal.classList.add('hidden'); editingCustomerId = null; });

addDeviceBtn.addEventListener('click', () => openDeviceModal());
closeDeviceModal.addEventListener('click', () => { addDeviceModal.classList.add('hidden'); editingDeviceId = null; });

// Forms
addCustomerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Saving...";
    const fields = {
        name: document.getElementById('customer-name').value.trim(),
        contact_name: document.getElementById('customer-contact-name').value.trim(),
        contact: document.getElementById('customer-contact').value.trim(),
        phone: document.getElementById('customer-phone').value.trim(),
        industry: document.getElementById('customer-industry').value,
        country: document.getElementById('customer-country').value.trim(),
        address: document.getElementById('customer-address').value.trim(),
        notes: document.getElementById('customer-notes').value.trim(),
    };
    try {
        if (editingCustomerId) {
            await updateDoc(doc(db, 'customers', editingCustomerId), { ...fields, updated_at: serverTimestamp() });
        } else {
            await addDoc(collection(db, "customers"), { ...fields, status: 'pending', created_at: serverTimestamp() });
        }
        editingCustomerId = null;
        e.target.reset();
        addCustomerModal.classList.add('hidden');
    } catch (err) {
        console.error("Error saving customer:", err);
        alert("Failed to save customer. See console.");
    } finally {
        btn.disabled = false;
        btn.textContent = ogText;
    }
});

addDeviceForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Saving...";
    try {
        if (editingDeviceId) {
            await updateDoc(doc(db, 'devices', editingDeviceId), {
                customer_id: document.getElementById('device-customer').value,
                location: document.getElementById('device-location').value.trim(),
                updated_at: serverTimestamp(),
            });
        } else {
            await addDoc(collection(db, "devices"), {
                device_id: document.getElementById('device-id-input').value.trim(),
                app_id: "unassigned",
                customer_id: document.getElementById('device-customer').value,
                location: document.getElementById('device-location').value.trim(),
                status: "active",
                registered_at: serverTimestamp()
            });
        }
        editingDeviceId = null;
        e.target.reset();
        addDeviceModal.classList.add('hidden');
    } catch (err) {
        console.error("Error saving device:", err);
        alert("Failed to save device. See console.");
    } finally {
        btn.disabled = false;
        btn.textContent = ogText;
    }
});

// ── Sync Events Listener ──────────────────────────────────────────────────────


// Fetch only mismatch/alert events for the Admin table
function initListener() {
    const q = query(
        collection(db, "sync_events"),
        where("event_type", "in", ["mismatch", "alert"]),
        orderBy("timestamp", "desc"),
        limit(50)
    );

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

// ── Users Tab ──────────────────────────────────────────────────────────────────

function initUsers() {
    const q = query(collection(db, 'users'), orderBy('created_at', 'desc'));
    unsubscribeUsers = onSnapshot(q, (snap) => {
        const users = snap.docs.map(d => ({ id: d.id, ...d.data() }));
        renderUsers(users);
    }, (err) => {
        console.error('Users listener error:', err);
    });
}

function renderUsers(users) {
    const pending = users.filter(u => !u.approved);
    const approved = users.filter(u => u.approved);

    const badge = document.getElementById('pending-users-badge');
    if (badge) {
        badge.textContent = pending.length;
        badge.classList.toggle('hidden', pending.length === 0);
    }

    const pendingEl = document.getElementById('users-pending-list');
    const approvedEl = document.getElementById('users-approved-list');

    pendingEl.innerHTML = pending.length
        ? pending.map(u => `
            <div class="flex items-center justify-between p-3 bg-amber-500/5 border border-amber-500/15 rounded-xl">
                <div class="min-w-0">
                    <p class="text-sm font-medium text-white truncate">${escapeHtml(u.email || u.id)}</p>
                    <p class="text-[10px] text-gray-500 mt-0.5">${u.display_name ? escapeHtml(u.display_name) + ' · ' : ''}Registered ${u.created_at?.toDate ? u.created_at.toDate().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '—'}</p>
                </div>
                <div class="flex items-center gap-2 shrink-0 ml-4">
                    <button onclick="window.approveUser('${u.id}')"
                        class="px-3 py-1.5 text-[11px] font-bold bg-green-500/15 text-green-400 border border-green-500/20 rounded-lg hover:bg-green-500 hover:text-white transition-all">
                        Approve
                    </button>
                    <button onclick="window.denyUser('${u.id}')"
                        class="px-3 py-1.5 text-[11px] font-bold bg-red-500/10 text-red-400 border border-red-500/20 rounded-lg hover:bg-red-500 hover:text-white transition-all">
                        Deny
                    </button>
                </div>
            </div>`)
            .join('')
        : '<p class="text-xs italic text-gray-600 py-4 text-center">No pending users.</p>';

    approvedEl.innerHTML = approved.length
        ? approved.map(u => `
            <div class="flex items-center justify-between p-3 bg-white/[0.02] border border-white/5 rounded-xl">
                <div class="min-w-0">
                    <p class="text-sm font-medium text-white truncate">${escapeHtml(u.email || u.id)}</p>
                    <p class="text-[10px] text-gray-500 mt-0.5">${u.display_name ? escapeHtml(u.display_name) + ' · ' : ''}Approved</p>
                </div>
                <button onclick="window.revokeUser('${u.id}')"
                    class="shrink-0 ml-4 px-3 py-1.5 text-[11px] font-bold bg-white/5 text-gray-400 border border-white/10 rounded-lg hover:bg-red-500/15 hover:text-red-400 hover:border-red-500/20 transition-all">
                    Revoke
                </button>
            </div>`)
            .join('')
        : '<p class="text-xs italic text-gray-600 py-4 text-center">No approved users.</p>';
}

window.approveUser = async function (uid) {
    try {
        await updateDoc(doc(db, 'users', uid), { approved: true });
    } catch (err) {
        console.error('Approve error:', err);
        alert('Failed to approve user.');
    }
};

window.revokeUser = async function (uid) {
    if (!confirm('Revoke access for this user?')) return;
    try {
        await updateDoc(doc(db, 'users', uid), { approved: false });
    } catch (err) {
        console.error('Revoke error:', err);
        alert('Failed to revoke user.');
    }
};

window.denyUser = async function (uid) {
    if (!confirm('Deny and remove this user request?')) return;
    try {
        await deleteDoc(doc(db, 'users', uid));
    } catch (err) {
        console.error('Deny error:', err);
        alert('Failed to deny user.');
    }
};

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
