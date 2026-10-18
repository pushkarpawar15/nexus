import { LoaderCircle } from 'lucide-react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';
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
      ? 'bg-primary text-white hover:bg-primary-hover'
      : variant === 'secondary'
        ? 'border border-line bg-white text-slate-700 hover:border-focus hover:bg-tint'
        : 'text-slate-600 hover:bg-elevated hover:text-slate-900';
  return (
    <button
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-xl px-4 text-sm font-semibold transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-60 ${tone} ${className}`}
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
      className={`rounded-2xl border border-line bg-panel shadow-sm ${className}`}
    >
      {children}
    </section>
  );
}
const tones: Record<string, string> = {
  neutral: 'border-slate-200 bg-slate-100 text-slate-600',
  success: 'border-green-200 bg-green-50 text-green-700',
  danger: 'border-red-200 bg-red-50 text-red-700',
  warning: 'border-amber-200 bg-amber-50 text-amber-700',
  info: 'border-blue-200 bg-blue-50 text-blue-700',
  ai: 'border-cyan-200 bg-cyan-50 text-cyan-700',
  human: 'border-violet-200 bg-violet-50 text-violet-700',
  hybrid: 'border-indigo-200 bg-indigo-50 text-indigo-700',
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
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-r-transparent" />
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
      <div className="mb-1 grid h-12 w-12 place-items-center rounded-2xl bg-tint text-primary">
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
      className="flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
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
  'mt-2 w-full rounded-xl border border-line bg-white px-3.5 py-3 text-sm text-slate-800 outline-none placeholder:text-slate-400 focus:border-primary focus:ring-2 focus:ring-[#93C5FD]';

export function MarkdownPreview({
  content,
  className = '',
}: {
  content: string;
  className?: string;
}) {
  return (
    <div
      className={`prose prose-sm max-w-none prose-headings:text-slate-900 prose-p:text-slate-700 prose-a:text-primary prose-code:text-slate-800 prose-pre:bg-elevated prose-pre:text-slate-800 prose-strong:text-slate-900 prose-blockquote:border-l-slate-300 prose-li:text-slate-700 prose-th:text-slate-900 prose-td:text-slate-700 ${className}`}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
      >
        {content || ''}
      </ReactMarkdown>
    </div>
  );
}
