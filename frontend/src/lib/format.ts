import type { Activity, Mode, Task, TaskStatus } from '../domain/types';
export const label = (value?: string | null) =>
  value
    ? value
        .toLowerCase()
        .replace(/[._-]+/g, ' ')
        .replace(/\b\w/g, (letter) => letter.toUpperCase())
    : '—';
export function fullDate(value?: string | null) {
  if (!value || Number.isNaN(Date.parse(value))) return '—';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}
export function relativeDate(value?: string | null) {
  if (!value || Number.isNaN(Date.parse(value))) return '—';
  const minutes = Math.max(
    0,
    Math.floor((Date.now() - Date.parse(value)) / 60000),
  );
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
export type Lane =
  | 'Backlog'
  | 'AI Queue'
  | 'Human Queue'
  | 'Hybrid Queue'
  | 'In Progress'
  | 'Validation'
  | 'Approval'
  | 'Needs Attention'
  | 'PR Review'
  | 'Shipped';
export const lanes: Lane[] = [
  'Backlog',
  'AI Queue',
  'Human Queue',
  'Hybrid Queue',
  'In Progress',
  'Validation',
  'Approval',
  'Needs Attention',
  'PR Review',
  'Shipped',
];
export function laneFor(task: Pick<Task, 'status' | 'mode'>): Lane {
  const { status, mode } = task;
  if (status === 'COMPLETED') return 'Shipped';
  if (status === 'PR_CREATED') return 'PR Review';
  if (['FAILED', 'BLOCKED', 'REJECTED'].includes(status))
    return 'Needs Attention';
  if (['AWAITING_APPROVAL', 'APPROVED', 'SECURITY_REVIEW'].includes(status))
    return 'Approval';
  if (status === 'VALIDATING') return 'Validation';
  if (status === 'IN_PROGRESS') return 'In Progress';
  if (status === 'HUMAN_QUEUE' || (status === 'READY' && mode === 'HUMAN'))
    return 'Human Queue';
  if (status === 'READY' && mode === 'HYBRID') return 'Hybrid Queue';
  if (status === 'READY' && mode === 'AI') return 'AI Queue';
  return 'Backlog';
}
export function statusTone(status: string) {
  if (['COMPLETED', 'SUCCEEDED', 'APPROVED'].includes(status)) return 'success';
  if (['FAILED', 'REJECTED', 'BLOCKED', 'CANCELLED'].includes(status))
    return 'danger';
  if (
    [
      'AWAITING_APPROVAL',
      'AWAITING_INPUT',
      'SECURITY_REVIEW',
      'HUMAN_QUEUE',
    ].includes(status)
  )
    return 'warning';
  if (
    [
      'ANALYZING',
      'IN_PROGRESS',
      'RUNNING',
      'VALIDATING',
      'PR_CREATED',
    ].includes(status)
  )
    return 'info';
  return 'neutral';
}
export function modeTone(mode?: Mode | null) {
  return mode === 'AI'
    ? 'ai'
    : mode === 'HUMAN'
      ? 'human'
      : mode === 'HYBRID'
        ? 'hybrid'
        : 'neutral';
}
export function approvalLabel(task: Task) {
  if (task.status === 'REJECTED') return 'Rejected';
  if (task.status === 'AWAITING_APPROVAL') return 'Awaiting decision';
  if (
    task.status === 'APPROVED' ||
    task.transition_history.some((x) => x.to_status === 'APPROVED')
  )
    return 'Approved';
  return task.approval_required ? 'Required later' : 'Not required';
}
export function taskActivity(task: Task): Activity[] {
  return task.transition_history.map((transition, index) => ({
    id: `${task.id}-${index}-${transition.at}`,
    task_id: task.id,
    execution_id: null,
    worker_name: transition.actor,
    event_type: 'workflow.transition',
    message: `${label(transition.from_status)} → ${label(transition.to_status)}. ${transition.reason}`,
    occurred_at: transition.at,
    source: 'SYSTEM',
    metadata: {},
  }));
}
export function dedupeActivity(events: Activity[]) {
  const unique = [
    ...new Map(events.map((event) => [event.id, event])).values(),
  ];
  return unique.sort(
    (a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at),
  );
}
export const isOpen = (status: TaskStatus) => status !== 'COMPLETED';
