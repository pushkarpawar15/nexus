import { createContext, useContext } from 'react';
export type ToastKind = 'success' | 'error' | 'info';
export const ToastContext = createContext<{
  showToast: (kind: ToastKind, message: string) => void;
} | null>(null);
export function useToast() {
  const value = useContext(ToastContext);
  if (!value) throw new Error('ToastProvider is missing');
  return value.showToast;
}
