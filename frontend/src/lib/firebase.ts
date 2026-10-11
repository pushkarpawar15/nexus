import { getApp, getApps, initializeApp } from 'firebase/app';
import {
  getAuth,
  connectAuthEmulator,
  type Auth,
  type User,
} from 'firebase/auth';

const env = import.meta.env;
const keys = [
  'VITE_FIREBASE_API_KEY',
  'VITE_FIREBASE_AUTH_DOMAIN',
  'VITE_FIREBASE_PROJECT_ID',
  'VITE_FIREBASE_APP_ID',
] as const;
export const firebaseConfigured = keys.every((key) => Boolean(env[key]));
export const authEmulator = Boolean(env.VITE_FIREBASE_AUTH_EMULATOR_URL);
let auth: Auth | null = null;
if (firebaseConfigured) {
  const app = getApps().length
    ? getApp()
    : initializeApp({
        apiKey: env.VITE_FIREBASE_API_KEY,
        authDomain: env.VITE_FIREBASE_AUTH_DOMAIN,
        projectId: env.VITE_FIREBASE_PROJECT_ID,
        appId: env.VITE_FIREBASE_APP_ID,
      });
  auth = getAuth(app);
  if (authEmulator) {
    const endpoint = env.VITE_FIREBASE_AUTH_EMULATOR_URL.startsWith('http')
      ? env.VITE_FIREBASE_AUTH_EMULATOR_URL
      : `http://${env.VITE_FIREBASE_AUTH_EMULATOR_URL}:${env.VITE_FIREBASE_AUTH_EMULATOR_PORT || '9099'}`;
    connectAuthEmulator(auth, endpoint, { disableWarnings: true });
  }
}
export { auth };
export async function getIdToken(user: User | null): Promise<string> {
  if (!auth || !user) throw new Error('Sign in to use the NEXUS API.');
  return user.getIdToken();
}
