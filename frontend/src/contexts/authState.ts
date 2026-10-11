import { createContext, useContext } from 'react';
import type { User } from 'firebase/auth';
export interface AuthValue {
  user: User | null;
  role: 'viewer' | 'operator' | 'admin' | null;
  loading: boolean;
  configured: boolean;
  emulator: boolean;
  signInGoogle: () => Promise<void>;
  signInEmail: (
    email: string,
    password: string,
    create: boolean,
  ) => Promise<void>;
  signOutUser: () => Promise<void>;
}
export const AuthContext = createContext<AuthValue | null>(null);
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('AuthProvider is missing');
  return value;
}
