import { getAuth, signInWithPopup, GoogleAuthProvider, signOut, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js";
import { app } from "./firebase-config.js";

const auth = getAuth(app);
const provider = new GoogleAuthProvider();

export function setupAuthUI(requireLoginCallback, loggedInCallback) {
    onAuthStateChanged(auth, (user) => {
        if (user) {
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
