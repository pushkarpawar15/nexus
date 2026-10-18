import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  createUserWithEmailAndPassword,
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
  type User,
} from 'firebase/auth';
import { auth, authEmulator, firebaseConfigured } from '../lib/firebase';
import { getCurrentIdentity } from '../lib/api';

import { AuthContext, type AuthValue } from './authState';
const isRole = (value: unknown): value is 'viewer' | 'operator' | 'admin' =>
  value === 'viewer' || value === 'operator' || value === 'admin';
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [role, setRole] = useState<'viewer' | 'operator' | 'admin' | null>(
    null,
  );
  const [loading, setLoading] = useState(firebaseConfigured);
  useEffect(() => {
    if (!auth) return;
    return onAuthStateChanged(auth, async (next) => {
      setUser(next);
      if (!next) setRole(null);
      else {
        try {
          const claim = (await next.getIdTokenResult()).claims.nexus_role;
          if (isRole(claim)) setRole(claim);
          else {
            // No custom claim yet: the API applies NEXUS_DEFAULT_ROLE, so mirror
            // the effective role it reports instead of guessing.
            try {
              const identity = await getCurrentIdentity(next);
              setRole(isRole(identity.role) ? identity.role : null);
            } catch {
              setRole(import.meta.env.PROD ? null : 'operator');
            }
          }
        } catch {
          setRole(import.meta.env.PROD ? null : 'operator');
        }
      }
      setLoading(false);
    });
  }, []);
  const value = useMemo<AuthValue>(
    () => ({
      user,
      role,
      loading,
      configured: firebaseConfigured,
      emulator: authEmulator,
      signInGoogle: async () => {
        if (!auth) throw new Error('Firebase is not configured.');
        await signInWithPopup(auth, new GoogleAuthProvider());
      },
      signInEmail: async (email, password, create) => {
        if (!auth) throw new Error('Firebase is not configured.');
        if (create) await createUserWithEmailAndPassword(auth, email, password);
        else await signInWithEmailAndPassword(auth, email, password);
      },
      signOutUser: async () => {
        if (auth) await signOut(auth);
      },
    }),
    [user, role, loading],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
