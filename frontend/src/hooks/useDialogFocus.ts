import { useEffect, type RefObject } from 'react';
export function useDialogFocus(
  open: boolean,
  dialog: RefObject<HTMLElement | null>,
  close: () => void,
) {
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const root = dialog.current;
    const focusable = () =>
      root?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]',
      ) ?? [];
    const first = focusable()[0];
    first?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
      if (event.key === 'Tab') {
        const all = [...focusable()];
        if (!all.length) return;
        const head = all[0];
        const tail = all[all.length - 1];
        if (event.shiftKey && document.activeElement === head) {
          event.preventDefault();
          tail?.focus();
        } else if (!event.shiftKey && document.activeElement === tail) {
          event.preventDefault();
          head?.focus();
        }
      }
    };
    document.addEventListener('keydown', handle);
    return () => {
      document.removeEventListener('keydown', handle);
      previous?.focus();
    };
  }, [open, dialog, close]);
}
