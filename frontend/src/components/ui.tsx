import { LoaderCircle } from 'lucide-react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
export function Button({
  variant = 'primary',
  loading = false,
  className = '',
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost';
  loading?: boolean;
}) {
  const tone =
    variant === 'primary'
      ? 'bg-mint text-canvas hover:brightness-105'
      : variant === 'secondary'
        ? 'border border-line bg-elevated text-zinc-100 hover:border-zinc-500'
        : 'text-zinc-300 hover:bg-elevated';
  return (
    <button
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-xl px-4 text-sm font-semibold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-mint disabled:cursor-not-allowed disabled:opacity-50 ${tone} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <LoaderCircle size={16} className="animate-spin" />}
      {children}
    </button>
  );
}
export function Panel({
  children,
  className = '',
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-2xl border border-line bg-panel shadow-[0_18px_60px_rgba(0,0,0,.12)] ${className}`}
    >
      {children}
    </section>
  );
}
const tones: Record<string, string> = {
  neutral: 'border-[#384150] bg-[#202733] text-[#B7C0CE]',
  success: 'border-[#345c39] bg-[#203725] text-[#B7F37C]',
  danger: 'border-[#653a42] bg-[#3a242c] text-[#FF9BA9]',
  warning: 'border-[#6a5636] bg-[#382f24] text-[#F7CA80]',
  info: 'border-[#30566b] bg-[#203644] text-[#80D8F1]',
  ai: 'border-[#3c5a4d] bg-[#1f3631] text-[#8DE8BC]',
  human: 'border-[#584a6e] bg-[#2f2940] text-[#C9B2FC]',
  hybrid: 'border-[#415675] bg-[#263448] text-[#A9C8F7]',
};
export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: keyof typeof tones;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-[.08em] ${tones[tone]}`}
    >
      {children}
    </span>
  );
}
export function LoadingState({
  label = 'Loading workspace…',
}: {
  label?: string;
}) {
  return (
    <div className="flex min-h-56 items-center justify-center gap-3 text-sm text-muted">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-mint border-r-transparent" />
      {label}
    </div>
  );
}
export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex min-h-64 flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-line px-6 text-center">
      <div className="mb-1 grid h-12 w-12 place-items-center rounded-2xl bg-elevated text-mint">
        N
      </div>
      <h3 className="font-display text-xl font-semibold">{title}</h3>
      <p className="max-w-lg text-sm leading-6 text-muted">{description}</p>
      {action}
    </div>
  );
}
export function ErrorState({
  message,
  retry,
}: {
  message: string;
  retry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex items-center justify-between gap-4 rounded-xl border border-rose-900/70 bg-rose-950/40 px-4 py-3 text-sm text-rose-200"
    >
      <span>{message}</span>
      {retry && (
        <Button variant="ghost" onClick={retry}>
          Try again
        </Button>
      )}
    </div>
  );
}
export const fieldClass =
  'mt-2 w-full rounded-xl border border-line bg-elevated px-3.5 py-3 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-mint focus:ring-2 focus:ring-mint/20';
