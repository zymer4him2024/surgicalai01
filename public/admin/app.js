import { getFirestore, collection, query, orderBy, limit, onSnapshot, where, doc, setDoc, addDoc, serverTimestamp } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js";
import { app } from "../firebase-config.js";
import { setupAuthUI, getCurrentUser } from "../auth.js";

const db = getFirestore(app);

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
    if (statusAgeInterval) { clearInterval(statusAgeInterval); statusAgeInterval = null; }
}

function loggedIn(user) {
    authOverlay.classList.add('hidden');
    mainContent.classList.remove('hidden');
    userEmailDisplay.innerText = user.email;
    initListener();
    initControlPanel();
    initStatusPanel();
    initTargetConfig();
    initCustomers();
    initDevices();
}

setupAuthUI(requireLogin, loggedIn);

// Modal Logic
closeModal.addEventListener('click', () => modal.classList.add('hidden'));
modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.classList.add('hidden');
});

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

function initStatusPanel() {
    if (unsubscribeStatus) unsubscribeStatus();
    if (statusAgeInterval) clearInterval(statusAgeInterval);

    const statusDocRef = doc(db, "system_status", "rpi");
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

const controlDocRef = doc(db, "device_control", "rpi");
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
    try {
        await setDoc(controlDocRef, {
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
    unsubscribeControl = onSnapshot(controlDocRef, (docSnap) => {
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

// ── DATA INFO — Preset Cycle Configuration ────────────────────────────────────

const SURGICAL_CLASSES = [
    'Overholt Clamp', 'Metz. Scissor', 'Sur. Scissor', 'Needle Holder',
    'Sur. Forceps', 'Atr. Forceps', 'Scalpel', 'Retractor',
    'Hook', 'Lig. Clamp', 'Peri. Clamp', 'Bowl', 'Tong',
];

const jobConfigDocRef = doc(db, 'job_config', 'rpi');
const jobStatusMsg = document.getElementById('job-status-msg');
const startInspectionBtn = document.getElementById('start-inspection-btn');
const clearTargetBtn = document.getElementById('clear-target-btn');

// Generate 5 random preset sets (0–2 per class) on page load
const PRESET_SETS = Array.from({ length: 5 }, (_, i) => ({
    label: `Set ${i + 1}`,
    counts: Object.fromEntries(SURGICAL_CLASSES.map(cls => [cls, Math.floor(Math.random() * 3)])),
}));

function renderPresetSets() {
    const container = document.getElementById('preset-sets-container');
    const rows = PRESET_SETS.map((set, idx) => {
        const nonZero = Object.entries(set.counts).filter(([, v]) => v > 0);
        const summary = nonZero.length
            ? nonZero.map(([k, v]) => `<span class="inline-block bg-white/5 border border-white/10 rounded-lg px-2 py-0.5 text-[10px] m-0.5 text-gray-400 font-mono">${k} ×${v}</span>`).join(' ')
            : '<span class="text-appleMuted italic text-xs ml-2">—</span>';
        return `<div class="preset-row flex items-center p-3 border-b border-white/5 last:border-0 hover:bg-white/[0.03] transition-all group" data-idx="${idx}">
            <div class="w-12 shrink-0 text-center">
                <span class="text-[10px] font-bold text-blue-500/60 group-hover:text-blue-500 transition-colors uppercase tracking-widest">${idx + 1}</span>
            </div>
            <div class="flex-1 min-w-0">
                <div class="flex flex-wrap items-center">
                    ${summary}
                </div>
            </div>
        </div>`;
    }).join('');

    container.innerHTML = `<div class="w-full">
        <div class="flex items-center px-4 py-2 bg-white/5 border-b border-white/10 rounded-t-xl">
            <span class="text-[9px] font-bold uppercase tracking-[0.2em] text-appleMuted w-12 text-center">Set</span>
            <span class="text-[9px] font-bold uppercase tracking-[0.2em] text-appleMuted">Configured Instruments</span>
        </div>
        <div class="divide-y divide-white/5">${rows}</div>
    </div>`;
}

function initTargetConfig() {
    renderPresetSets();
}

async function startInspection() {
    startInspectionBtn.disabled = true;
    jobStatusMsg.textContent = 'Starting cycle…';
    try {
        await setDoc(jobConfigDocRef, {
            sets: PRESET_SETS.map(s => s.counts),
            cursor: 0,
            ts: serverTimestamp(),
        });
        jobStatusMsg.textContent = 'Cycle started — Set 1 active';
    } catch (e) {
        console.error('startInspection error:', e);
        jobStatusMsg.textContent = 'Error: ' + e.message;
    } finally {
        startInspectionBtn.disabled = false;
    }
}

function clearTarget() {
    jobStatusMsg.textContent = '';
}

startInspectionBtn.addEventListener('click', startInspection);
clearTargetBtn.addEventListener('click', clearTarget);

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

function renderCustomers() {
    if (customersData.length === 0) {
        customersTableBody.innerHTML = '<tr><td colspan="2" class="py-4 text-center text-appleMuted italic text-xs">No customers found.</td></tr>';
        return;
    }
    customersTableBody.innerHTML = customersData.map(c => `
        <tr class="group hover:bg-white/[0.03] transition-all">
            <td class="py-3 font-medium text-white">${c.name}</td>
            <td class="py-3 text-appleMuted text-xs flex items-center justify-between">
                ${c.contact}
                <span class="text-[10px] text-gray-500 opacity-0 group-hover:opacity-100 transition-opacity font-mono">${c.id}</span>
            </td>
        </tr>
    `).join('');
}

function updateDeviceCustomerSelect() {
    deviceCustomerSelect.innerHTML = '<option value="" disabled selected>Select Customer</option>' +
        customersData.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
}

// Device Listeners & Rendering
function initDevices() {
    const q = query(collection(db, "devices"), orderBy("device_id", "asc"));
    unsubscribeDevices = onSnapshot(q, (snapshot) => {
        const devicesData = [];
        snapshot.forEach((doc) => {
            devicesData.push({ id: doc.id, ...doc.data() });
        });
        renderDevices(devicesData);
    }, (error) => console.error("Error fetching devices:", error));
}

function renderDevices(devicesData) {
    if (devicesData.length === 0) {
        devicesTableBody.innerHTML = '<tr><td colspan="3" class="py-4 text-center text-appleMuted italic text-xs">No devices found.</td></tr>';
        return;
    }
    devicesTableBody.innerHTML = devicesData.map(d => {
        const customer = customersData.find(c => c.id === d.customer_id);
        const customerName = customer ? customer.name : `<span class="italic text-gray-500">Unknown (${d.customer_id})</span>`;
        return `
        <tr class="group hover:bg-white/[0.03] transition-all">
            <td class="py-3 font-mono font-medium text-white text-xs">${d.device_id}</td>
            <td class="py-3 text-appleMuted text-xs">${customerName}</td>
            <td class="py-3 text-gray-400 text-xs">${d.location}</td>
        </tr>
    `;
    }).join('');
}

// Modals
addCustomerBtn.addEventListener('click', () => addCustomerModal.classList.remove('hidden'));
closeCustomerModal.addEventListener('click', () => addCustomerModal.classList.add('hidden'));
addCustomerModal.addEventListener('click', (e) => { if (e.target === addCustomerModal) addCustomerModal.classList.add('hidden'); });

addDeviceBtn.addEventListener('click', () => addDeviceModal.classList.remove('hidden'));
closeDeviceModal.addEventListener('click', () => addDeviceModal.classList.add('hidden'));
addDeviceModal.addEventListener('click', (e) => { if (e.target === addDeviceModal) addDeviceModal.classList.add('hidden'); });

// Forms
addCustomerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    const ogText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Saving...";
    try {
        await addDoc(collection(db, "customers"), {
            name: document.getElementById('customer-name').value.trim(),
            contact: document.getElementById('customer-contact').value.trim(),
            created_at: serverTimestamp()
        });
        e.target.reset();
        addCustomerModal.classList.add('hidden');
    } catch (err) {
        console.error("Error adding customer:", err);
        alert("Failed to add customer. See console.");
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
    btn.textContent = "Registering...";
    try {
        await addDoc(collection(db, "devices"), {
            device_id: document.getElementById('device-id').value.trim(),
            customer_id: document.getElementById('device-customer').value,
            location: document.getElementById('device-location').value.trim(),
            status: "active",
            registered_at: serverTimestamp()
        });
        e.target.reset();
        addDeviceModal.classList.add('hidden');
    } catch (err) {
        console.error("Error adding device:", err);
        alert("Failed to add device. See console.");
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
