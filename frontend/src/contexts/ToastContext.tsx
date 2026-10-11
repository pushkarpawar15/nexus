import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { Check, Info, X, XCircle } from 'lucide-react';
import { ToastContext, type ToastKind } from './toastState';

interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const showToast = useCallback((kind: ToastKind, message: string) => {
    const id = Date.now() + Math.random();
    setItems((old) => [...old, { id, kind, message }]);
    window.setTimeout(
      () => setItems((old) => old.filter((item) => item.id !== id)),
      5500,
    );
  }, []);
  const value = useMemo(() => ({ showToast }), [showToast]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        className="fixed bottom-5 right-5 z-[80] grid gap-2"
      >
        {items.map((item) => (
          <div
            key={item.id}
            className="flex max-w-sm items-center gap-3 rounded-xl border border-line bg-panel px-4 py-3 shadow-xl"
          >
            <span
              className={item.kind === 'error' ? 'text-rose-300' : 'text-mint'}
            >
              {item.kind === 'success' ? (
                <Check size={18} />
              ) : item.kind === 'error' ? (
                <XCircle size={18} />
              ) : (
                <Info size={18} />
              )}
            </span>
            <span className="text-sm">{item.message}</span>
            <button
              aria-label="Dismiss notification"
              onClick={() =>
                setItems((old) => old.filter((x) => x.id !== item.id))
              }
            >
              <X size={15} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
