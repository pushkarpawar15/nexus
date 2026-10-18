import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from './AuthContext';
import { useAuth } from './authState';

const mocks = vi.hoisted(() => ({
  onAuthStateChanged: vi.fn(),
  getCurrentIdentity: vi.fn(),
}));

vi.mock('../lib/firebase', () => ({
  auth: {},
  authEmulator: false,
  firebaseConfigured: true,
  getIdToken: vi.fn(),
}));

vi.mock('firebase/auth', () => ({
  onAuthStateChanged: mocks.onAuthStateChanged,
  createUserWithEmailAndPassword: vi.fn(),
  GoogleAuthProvider: vi.fn(),
  signInWithEmailAndPassword: vi.fn(),
  signInWithPopup: vi.fn(),
  signOut: vi.fn(),
}));

vi.mock('../lib/api', () => ({
  getCurrentIdentity: mocks.getCurrentIdentity,
}));

function RoleProbe() {
  const { role } = useAuth();
  return <span data-testid="role">{role ?? 'none'}</span>;
}

const signIn = (claims: Record<string, unknown>) => {
  const user = { getIdTokenResult: async () => ({ claims }) };
  mocks.onAuthStateChanged.mockImplementation((_auth, callback) => {
    void callback(user);
    return () => {};
  });
};

const renderRole = () =>
  render(
    <AuthProvider>
      <RoleProbe />
    </AuthProvider>,
  );

describe('role resolution', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(cleanup);

  it('mirrors the server role when the account has no nexus_role claim', async () => {
    signIn({});
    mocks.getCurrentIdentity.mockResolvedValue({ uid: 'u1', role: 'admin' });

    renderRole();

    await waitFor(() =>
      expect(screen.getByTestId('role').textContent).toBe('admin'),
    );
  });

  it('prefers the nexus_role claim over the server role', async () => {
    signIn({ nexus_role: 'viewer' });

    renderRole();

    await waitFor(() =>
      expect(screen.getByTestId('role').textContent).toBe('viewer'),
    );
    expect(mocks.getCurrentIdentity).not.toHaveBeenCalled();
  });

  it('falls back to operator when the API is unreachable outside production', async () => {
    signIn({});
    mocks.getCurrentIdentity.mockRejectedValue(new Error('offline'));

    renderRole();

    await waitFor(() =>
      expect(screen.getByTestId('role').textContent).toBe('operator'),
    );
  });
});
