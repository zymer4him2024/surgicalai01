import { getAuth, signInWithPopup, GoogleAuthProvider, signOut, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js";
import { getFirestore, doc, getDoc, setDoc, serverTimestamp } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js";
import { app } from "./firebase-config.js";

const auth = getAuth(app);
const db = getFirestore(app);
const provider = new GoogleAuthProvider();

async function upsertUserDoc(user) {
    const ref = doc(db, 'users', user.uid);
    const snap = await getDoc(ref);
    const data = {
        email: user.email,
        display_name: user.displayName,
        photo_url: user.photoURL,
        last_login: serverTimestamp(),
    };
    if (!snap.exists()) {
        data.created_at = serverTimestamp();
    }
    await setDoc(ref, data, { merge: true });
}

export function setupAuthUI(requireLoginCallback, loggedInCallback) {
    onAuthStateChanged(auth, (user) => {
        if (user) {
            upsertUserDoc(user).catch(err => console.warn('upsertUserDoc failed:', err));
            loggedInCallback(user);
        } else {
            requireLoginCallback();
        }
    });

    document.querySelectorAll('.login-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            signInWithPopup(auth, provider).catch(error => {
                if (error.code !== 'auth/popup-closed-by-user') {
                    console.error("Sign in error:", error);
                }
            });
        });
    });

    document.querySelectorAll('.logout-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            signOut(auth).catch(error => console.error("Sign Out Error", error));
        });
    });
}

export function getCurrentUser() {
    return auth.currentUser;
}
