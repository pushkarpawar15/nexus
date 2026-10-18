import { useCallback, useRef, useState } from 'react';
import { useAuth } from '../contexts/authState';
import { useDialogFocus } from '../hooks/useDialogFocus';
import { Button, fieldClass } from './ui';
export function AuthDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const close = useCallback(onClose, [onClose]);
  useDialogFocus(open, root, close);
  const auth = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [create, setCreate] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  if (!open) return null;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await auth.signInEmail(email, password, create);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Sign in failed.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="fixed inset-0 z-[70] grid place-items-center bg-black/40 p-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        ref={root}
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        className="w-full max-w-md rounded-2xl border border-line bg-panel p-6 shadow-xl"
      >
        <div className="flex items-start justify-between">
          <div>
            <p className="eyebrow">IDENTITY</p>
            <h2
              id="auth-title"
              className="mt-2 font-display text-2xl font-bold"
            >
              Sign in to NEXUS
            </h2>
          </div>
          <button aria-label="Close" className="text-muted" onClick={onClose}>
            ✕
          </button>
        </div>
        {auth.emulator ? (
          <form className="mt-6 grid gap-4" onSubmit={submit}>
            <label className="field-label">
              Email
              <input
                className={fieldClass}
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="field-label">
              Password
              <input
                className={fieldClass}
                type="password"
                minLength={6}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-red-600">
                {error}
              </p>
            )}
            <Button loading={busy} type="submit">
              {create ? 'Create account' : 'Sign in'}
            </Button>
            <button
              type="button"
              className="text-sm text-muted underline"
              onClick={() => setCreate(!create)}
            >
              {create
                ? 'Already have an account? Sign in'
                : 'Create a local emulator account'}
            </button>
          </form>
        ) : (
          <div className="mt-6">
            {!auth.configured ? (
              <p className="text-sm leading-6 text-muted">
                Firebase is not configured. Set the public VITE_FIREBASE_*
                values to enable sign in.
              </p>
            ) : (
              <Button
                className="w-full"
                onClick={() =>
                  void auth
                    .signInGoogle()
                    .then(onClose)
                    .catch((e) => setError(String(e)))
                }
              >
                Continue with Google
              </Button>
            )}
            {error && (
              <p role="alert" className="mt-3 text-sm text-red-600">
                {error}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
