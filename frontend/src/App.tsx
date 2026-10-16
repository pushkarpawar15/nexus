import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react';
import {
  Activity as ActivityIcon,
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  GitPullRequest,
  GitBranch,
  Copy,
  Pencil,
  Paperclip,
  Download,
  Trash2,
  Plus,
  RefreshCw,
  Shield,
  Sparkles,
  Square,
  Users,
  Zap,
} from 'lucide-react';
import {
  Link,
  Navigate,
  Route,
  Routes,
  useOutletContext,
  useNavigate,
  useParams,
} from 'react-router-dom';
import type {
  Activity,
  Execution,
  ExecutionDiff,
  ExecutionPlanInput,
  ModelChoices,
  Mode,
  Risk,
  Task,
} from './domain/types';
import { useAuth } from './contexts/authState';
import { useToast } from './contexts/toastState';
import { useTasks } from './hooks/useTasks';
import {
  analyzeTask,
  answerWorkflowQuestion,
  createPullRequest,
  syncPullRequest,
  cloneTaskPlan,
  decideApproval,
  getPullRequest,
  getPullRequestEligibility,
  getExecutionDiff,
  getSecurityReview,
  getTask,
  getModelChoices,
  getValidationReport,
  listApprovals,
  beginGitHubInstall,
  connectGitHubPAT,
  deleteTaskAttachment,
  disconnectGitHub,
  downloadTaskAttachment,
  getRepositoryRemovalImpact,
  getGitHubConnection,
  listRepositories,
  removeRepository,
  listRecentActivity,
  listTaskExecutions,
  listTaskHistory,
  retryExecution,
  startExecution,
  stopExecution,
  updateTaskPlan,
  setTaskMode,
  uploadTaskAttachments,
} from './lib/api';
import {
  approvalLabel,
  dedupeActivity,
  fullDate,
  label,
  laneFor,
  lanes,
  modeTone,
  relativeDate,
  statusTone,
  taskActivity,
} from './lib/format';
import { AppShell } from './components/AppShell';
import { SkillsPage } from './components/SkillsPage';
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
} from './components/ui';

type ShellContext = { openCreate: () => void };
function Heading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="mt-2 font-display text-3xl font-bold tracking-tight sm:text-4xl">
          {title}
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          {description}
        </p>
      </div>
      {action}
    </div>
  );
}
function TaskCard({ task }: { task: Task }) {
  return (
    <Link
      to={`/tasks/${task.id}`}
      className="block rounded-2xl border border-line bg-panel p-4 transition hover:-translate-y-0.5 hover:border-zinc-500"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs text-muted">
          {task.repository_label || task.repository_key}
        </span>
        <Badge tone={statusTone(task.status)}>{label(task.status)}</Badge>
      </div>
      <h3 className="mt-3 line-clamp-1 font-semibold">{task.title}</h3>
      <p className="mt-1 line-clamp-2 min-h-10 text-sm leading-5 text-muted">
        {task.description}
      </p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Badge tone={modeTone(task.mode)}>{label(task.mode)}</Badge>
        <Badge
          tone={
            task.risk === 'HIGH' || task.risk === 'CRITICAL'
              ? 'danger'
              : task.risk === 'MEDIUM'
                ? 'warning'
                : task.risk
                  ? 'success'
                  : 'neutral'
          }
        >
          {label(task.risk)}
        </Badge>
        <span className="ml-auto text-xs text-muted">
          {relativeDate(task.updated_at)}
        </span>
      </div>
      <p className="mt-3 text-xs text-muted">
        {task.assigned_agents.length
          ? task.assigned_agents.map(label).join(' · ')
          : 'No workers assigned'}
        {task.approval_required ? ' · Approval required' : ''}
      </p>
    </Link>
  );
}
function ActivityList({ events }: { events: Activity[] }) {
  if (!events.length)
    return (
      <EmptyState
        title="No activity yet"
        description="Workflow events will appear here as tasks move through the system."
      />
    );
  return (
    <div className="divide-y divide-line">
      {events.map((event) => (
        <div key={event.id} className="flex gap-3 py-4 first:pt-0 last:pb-0">
          <span
            className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${/failed|blocked/i.test(event.event_type) ? 'bg-rose-400' : 'bg-mint'}`}
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold">
                {label(event.event_type)}
              </span>
              <Badge>{label(event.source)}</Badge>
              <span className="ml-auto text-xs text-muted">
                {fullDate(event.occurred_at)}
              </span>
            </div>
            <p className="mt-1 text-sm leading-6 text-muted">{event.message}</p>
            {Object.keys(event.metadata).length > 0 && (
              <details className="mt-2 text-xs text-muted">
                <summary className="cursor-pointer">Details</summary>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-elevated p-3 font-mono text-[11px] leading-5 text-zinc-300">
                  {JSON.stringify(event.metadata, null, 2)}
                </pre>
              </details>
            )}
            <p className="mt-1 text-xs text-muted">
              {label(event.worker_name)} ·{' '}
              <Link
                className="text-mint hover:underline"
                to={`/tasks/${event.task_id}`}
              >
                View task
              </Link>
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}

function CommandCenter() {
  const { openCreate } = useOutletContext<ShellContext>();
  const { tasks, loading, error, refresh } = useTasks(30_000);
  const ordered = useMemo(
    () =>
      [...tasks].sort(
        (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at),
      ),
    [tasks],
  );
  const metrics = [
    [
      'Tasks in motion',
      tasks.filter((t) =>
        ['ANALYZING', 'IN_PROGRESS', 'VALIDATING', 'SECURITY_REVIEW'].includes(
          t.status,
        ),
      ).length,
      Zap,
    ],
    [
      'Needs approval',
      tasks.filter((t) => t.status === 'AWAITING_APPROVAL').length,
      Shield,
    ],
    [
      'Needs input',
      tasks.filter((t) => Boolean(t.pending_question)).length,
      CircleAlert,
    ],
    [
      'Shipped',
      tasks.filter((t) => t.status === 'COMPLETED').length,
      CheckCircle2,
    ],
    [
      'High risk open',
      tasks.filter(
        (t) =>
          ['HIGH', 'CRITICAL'].includes(t.risk || '') &&
          t.status !== 'COMPLETED',
      ).length,
      CircleAlert,
    ],
  ] as const;
  const inMotion = tasks.filter(
    (t) => !['COMPLETED', 'REJECTED'].includes(t.status),
  ).length;
  return (
    <>
      <Heading
        eyebrow="OPERATIONS / OVERVIEW"
        title="Command Center"
        description="A clear view of engineering work, worker assignments, and the decisions that need human attention."
        action={
          <Button onClick={openCreate}>
            <Plus size={16} />
            Create task
          </Button>
        }
      />
      <section className="relative mb-7 overflow-hidden rounded-3xl border border-mint/20 bg-gradient-to-br from-[#192a1c] via-[#121a19] to-[#10151d] p-7 sm:p-10">
        <div className="absolute -right-8 -top-16 h-64 w-64 rounded-full border border-mint/10" />
        <div className="absolute right-16 top-20 h-36 w-36 rounded-full border border-mint/10" />
        <div className="absolute right-28 top-32 h-12 w-12 rounded-full bg-mint/5" />
        <span className="absolute right-12 top-3 select-none font-display text-[220px] font-bold leading-none text-white/[.025]">
          N
        </span>
        <div className="relative max-w-3xl">
          <p className="eyebrow">THE AI-HUMAN ENGINEERING WORKFORCE</p>
          <h2 className="mt-3 font-display text-3xl font-bold leading-tight sm:text-5xl">
            From AI as a tool to AI as a teammate.
          </h2>
          <p className="mt-4 max-w-xl text-sm leading-6 text-zinc-300">
            Coordinate specialized workers, keep every handoff visible, and make
            human judgment part of the workflow.
          </p>
          <Link
            to="/board"
            className="mt-6 inline-flex items-center gap-2 text-sm font-bold text-mint"
          >
            Explore workforce board <ArrowRight size={16} />
          </Link>
        </div>
      </section>
      {error && (
        <div className="mb-5">
          <ErrorState message={error} retry={() => void refresh()} />
        </div>
      )}
      {loading ? (
        <LoadingState />
      ) : tasks.length === 0 ? (
        <EmptyState
          title="Your workspace is ready"
          description="Create the first engineering task and choose one of the configured repositories."
          action={<Button onClick={openCreate}>Create task</Button>}
        />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {metrics.map(([name, count, Icon]) => (
              <Panel key={name} className="p-5">
                <div className="flex items-center justify-between text-sm text-muted">
                  <span>{name}</span>
                  <Icon size={17} className="text-mint" />
                </div>
                <p className="mt-5 font-display text-3xl font-bold">{count}</p>
                <p className="mt-1 text-xs text-muted">
                  of {tasks.length} total tasks
                </p>
              </Panel>
            ))}
          </div>
          <div className="mt-5 grid gap-5 xl:grid-cols-[1.3fr_.7fr]">
            <Panel className="p-5 sm:p-6">
              <p className="eyebrow">LIVE PIPELINE</p>
              <h2 className="mt-2 font-display text-xl font-semibold">
                Work across the workflow
              </h2>
              <div className="mt-5 grid gap-3">
                {lanes.map((lane) => {
                  const count = tasks.filter(
                    (task) => laneFor(task) === lane,
                  ).length;
                  return (
                    <div
                      key={lane}
                      className="grid grid-cols-[120px_1fr_28px] items-center gap-3 text-xs"
                    >
                      <span className="text-muted">{lane}</span>
                      <div className="h-2 overflow-hidden rounded-full bg-elevated">
                        <div
                          className="h-full rounded-full bg-mint"
                          style={{
                            width: `${tasks.length ? (count / tasks.length) * 100 : 0}%`,
                          }}
                        />
                      </div>
                      <span className="text-right font-semibold">{count}</span>
                    </div>
                  );
                })}
              </div>
              <p className="mt-5 text-xs text-muted">
                {inMotion} tasks are currently open across the workforce.
              </p>
            </Panel>
            <Panel className="p-5 sm:p-6">
              <p className="eyebrow">WORKFORCE MIX</p>
              <h2 className="mt-2 font-display text-xl font-semibold">
                Who is assigned
              </h2>
              {(['AI', 'HUMAN', 'HYBRID'] as const).map((mode) => {
                const total = tasks.filter((task) => task.mode).length;
                const count = tasks.filter((task) => task.mode === mode).length;
                return (
                  <div key={mode} className="mt-5">
                    <div className="mb-2 flex justify-between text-xs">
                      <span>{label(mode)}</span>
                      <span className="text-muted">{count}</span>
                    </div>
                    <div className="h-2 rounded-full bg-elevated">
                      <div
                        className="h-2 rounded-full bg-violet"
                        style={{
                          width: `${total ? (count / total) * 100 : 0}%`,
                        }}
                      />
                    </div>
                  </div>
                );
              })}
              <div className="mt-7 rounded-xl border border-line bg-elevated/50 p-3 text-xs leading-5 text-muted">
                <Users size={15} className="mb-2 text-mint" />
                Workers are assigned only after the analysis and deterministic
                policy checks.
              </div>
            </Panel>
          </div>
          <div className="mb-3 mt-8 flex items-center justify-between">
            <div>
              <p className="eyebrow">LATEST WORK</p>
              <h2 className="mt-1 font-display text-xl font-semibold">
                Recently updated
              </h2>
            </div>
            <Link to="/tasks" className="text-sm text-mint">
              All tasks <ArrowRight className="ml-1 inline" size={14} />
            </Link>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {ordered.slice(0, 4).map((task) => (
              <TaskCard key={task.id} task={task} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

function WorkforceBoard() {
  const { openCreate } = useOutletContext<ShellContext>();
  const { tasks, loading, error, refresh } = useTasks(20_000);
  return (
    <>
      <Heading
        eyebrow="WORKFORCE / FLOW"
        title="Workforce Board"
        description="Follow work through AI, human, hybrid, validation, and review lanes."
        action={
          <div className="flex gap-2">
            <Button
              variant="secondary"
              aria-label="Refresh tasks"
              onClick={() => void refresh()}
            >
              <RefreshCw size={16} />
            </Button>
            <Button onClick={openCreate}>
              <Plus size={16} />
              New task
            </Button>
          </div>
        }
      />
      {error && (
        <div className="mb-4">
          <ErrorState message={error} retry={() => void refresh()} />
        </div>
      )}
      {loading ? (
        <LoadingState />
      ) : tasks.length === 0 ? (
        <EmptyState
          title="No work on the board yet"
          description="New tasks will appear here after they are added to the workforce."
          action={<Button onClick={openCreate}>Create task</Button>}
        />
      ) : (
        <div className="-mx-4 overflow-x-auto px-4 pb-5">
          <div className="flex min-w-max gap-3">
            {lanes.map((lane) => {
              const cards = tasks.filter((task) => laneFor(task) === lane);
              return (
                <section
                  key={lane}
                  className="w-[280px] rounded-2xl border border-line bg-[#0e1218] p-3"
                >
                  <div className="mb-3 flex items-center justify-between">
                    <h2 className="text-sm font-semibold">{lane}</h2>
                    <Badge>{cards.length}</Badge>
                  </div>
                  <div className="grid content-start gap-2">
                    {cards.length ? (
                      cards.map((task) => (
                        <TaskCard key={task.id} task={task} />
                      ))
                    ) : (
                      <div className="rounded-xl border border-dashed border-line p-5 text-center text-xs text-muted">
                        No work in this lane
                      </div>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}

function TasksPage() {
  const { openCreate } = useOutletContext<ShellContext>();
  const { tasks, loading, error, refresh } = useTasks(0);
  const [filter, setFilter] = useState('ALL');
  const visible = tasks.filter(
    (task) => filter === 'ALL' || task.status === filter,
  );
  return (
    <>
      <Heading
        eyebrow="WORK / INVENTORY"
        title="Tasks"
        description="Search and review every task submitted to the engineering workforce."
        action={
          <Button onClick={openCreate}>
            <Plus size={16} />
            New task
          </Button>
        }
      />
      <div className="mb-5 flex flex-wrap gap-2">
        {[
          'ALL',
          'BACKLOG',
          'READY',
          'IN_PROGRESS',
          'AWAITING_APPROVAL',
          'BLOCKED',
          'COMPLETED',
        ].map((status) => (
          <button
            onClick={() => setFilter(status)}
            className={`rounded-full border px-3 py-2 text-xs ${filter === status ? 'border-mint/40 bg-mint/10 text-mint' : 'border-line text-muted'}`}
            key={status}
          >
            {label(status)}
          </button>
        ))}
      </div>
      {error && (
        <div className="mb-4">
          <ErrorState message={error} retry={() => void refresh()} />
        </div>
      )}
      {loading ? (
        <LoadingState />
      ) : visible.length ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visible.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No matching tasks"
          description="Try another status filter or create a task to get started."
          action={<Button onClick={openCreate}>Create task</Button>}
        />
      )}
    </>
  );
}

function ActivityPage() {
  const { user } = useAuth();
  const { tasks, loading: taskLoading } = useTasks(0);
  const [events, setEvents] = useState<Activity[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    try {
      const direct = await listRecentActivity(user, 100);
      setEvents(
        dedupeActivity([
          ...direct,
          ...tasks.flatMap((task) =>
            task.transition_history.map((transition, index) => ({
              id: `${task.id}-${index}`,
              task_id: task.id,
              execution_id: null,
              worker_name: transition.actor,
              event_type: 'workflow.transition',
              message: `${label(transition.from_status)} → ${label(transition.to_status)}. ${transition.reason}`,
              occurred_at: transition.at,
              source: 'SYSTEM' as const,
              metadata: {},
            })),
          ),
        ]),
      );
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load activity.');
    } finally {
      setLoading(false);
    }
  }, [user, tasks]);
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 20_000);
    return () => window.clearInterval(id);
  }, [load]);
  return (
    <>
      <Heading
        eyebrow="WORKFORCE / SIGNAL"
        title="Agent Activity"
        description="A chronological feed of analysis, execution, validation, security, and human decisions."
        action={
          <Button variant="secondary" onClick={() => void load()}>
            <RefreshCw size={16} />
            Refresh
          </Button>
        }
      />
      {(error || (!taskLoading && error)) && (
        <div className="mb-4">
          <ErrorState message={error} retry={() => void load()} />
        </div>
      )}
      {loading ? (
        <LoadingState />
      ) : (
        <Panel className="p-5 sm:p-7">
          <ActivityList events={events} />
        </Panel>
      )}
    </>
  );
}

function ApprovalsPage() {
  const { user, role } = useAuth();
  const { tasks, loading, error, refresh } = useTasks(20_000);
  const toast = useToast();
  const [records, setRecords] = useState<
    Array<{
      task: Task;
      execution: Execution;
      approvals: Awaited<ReturnType<typeof listApprovals>>;
    }>
  >([]);
  const [reason, setReason] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState('');
  const load = useCallback(async () => {
    const awaiting = tasks.filter((t) => t.status === 'AWAITING_APPROVAL');
    const result = await Promise.all(
      awaiting.map(async (task) => {
        const [executions, approvals] = await Promise.all([
          listTaskExecutions(user, task.id),
          listApprovals(user, task.id),
        ]);
        const execution = executions.at(-1);
        return execution ? { task, execution, approvals } : null;
      }),
    );
    setRecords(result.filter((x): x is NonNullable<typeof x> => x !== null));
  }, [tasks, user]);
  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);
  async function decide(
    task: Task,
    execution: Execution,
    status: 'APPROVED' | 'REJECTED',
  ) {
    const why = reason[execution.id]?.trim();
    if (!why) {
      toast('error', 'Add a reason before recording this decision.');
      return;
    }
    setBusy(execution.id);
    try {
      await decideApproval(user, task.id, execution.id, {
        status,
        reason: why,
      });
      toast('success', `Decision ${label(status).toLowerCase()} and recorded.`);
      await refresh();
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Decision failed.');
    } finally {
      setBusy('');
    }
  }
  return (
    <>
      <Heading
        eyebrow="HUMAN OVERSIGHT / DECISIONS"
        title="Approvals"
        description="Review patch-bound evidence before approving or rejecting work."
        action={
          <Button variant="secondary" onClick={() => void refresh()}>
            <RefreshCw size={16} />
            Refresh
          </Button>
        }
      />
      {error && (
        <div className="mb-4">
          <ErrorState message={error} retry={() => void refresh()} />
        </div>
      )}
      {loading ? (
        <LoadingState />
      ) : records.length ? (
        <div className="grid gap-4">
          {records.map(({ task, execution }) => (
            <Panel key={execution.id} className="p-5 sm:p-6">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={statusTone(task.status)}>
                  {label(task.status)}
                </Badge>
                <Badge tone={modeTone(task.mode)}>{label(task.mode)}</Badge>
                <span className="text-xs text-muted">
                  {task.repository_label || task.repository_key}
                </span>
              </div>
              <Link
                className="mt-3 block font-display text-xl font-semibold hover:text-mint"
                to={`/tasks/${task.id}`}
              >
                {task.title}
              </Link>
              <p className="mt-2 text-sm leading-6 text-muted">
                {task.description}
              </p>
              <p className="mt-3 text-xs text-muted">
                Execution {execution.id} · patch{' '}
                {execution.patch_fingerprint?.slice(0, 12) || 'missing'}
              </p>
              {role === 'admin' ? (
                <>
                  <textarea
                    aria-label="Decision reason"
                    className="mt-4 w-full rounded-xl border border-line bg-elevated p-3 text-sm"
                    rows={2}
                    placeholder="Reason for your decision"
                    value={reason[execution.id] || ''}
                    onChange={(e) =>
                      setReason((old) => ({
                        ...old,
                        [execution.id]: e.target.value,
                      }))
                    }
                  />
                  <div className="mt-3 flex justify-end gap-2">
                    <Button
                      variant="secondary"
                      loading={busy === execution.id}
                      disabled={!reason[execution.id]?.trim()}
                      onClick={() => void decide(task, execution, 'REJECTED')}
                    >
                      Reject patch
                    </Button>
                    <Button
                      loading={busy === execution.id}
                      disabled={!reason[execution.id]?.trim()}
                      onClick={() => void decide(task, execution, 'APPROVED')}
                    >
                      Approve and publish PR
                    </Button>
                  </div>
                </>
              ) : (
                <p className="mt-4 text-xs text-muted">
                  An administrator must record the decision for this patch.
                </p>
              )}
            </Panel>
          ))}
        </div>
      ) : (
        <EmptyState
          title="No decisions are waiting"
          description="When a completed execution requires human approval, its evidence will appear here."
        />
      )}
    </>
  );
}

function RepositoriesPage() {
  const { user } = useAuth();
  const toast = useToast();
  const [connection, setConnection] = useState<{
    configured: boolean;
    connected: boolean;
    account_login: string | null;
    mode?: 'app' | 'pat' | null;
    repository?: string | null;
    repository_info?: {
      key: string;
      label: string;
      owner?: string;
      repo?: string;
      base_branch?: string;
      private?: boolean;
    } | null;
    pat_supported?: boolean;
  } | null>(null);
  const [repositories, setRepositories] = useState<
    Awaited<ReturnType<typeof listRepositories>>['items']
  >([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pat, setPat] = useState('');
  const [repositoryName, setRepositoryName] = useState('');
  const load = useCallback(async () => {
    setLoading(true);
    const [statusResult, reposResult] = await Promise.allSettled([
      getGitHubConnection(user),
      listRepositories(user),
    ]);
    let status: Awaited<ReturnType<typeof getGitHubConnection>> | null = null;
    const errors: string[] = [];
    if (statusResult.status === 'fulfilled') {
      status = statusResult.value;
      setConnection(status);
    } else {
      setConnection(null);
      errors.push('Could not load GitHub connection status.');
    }
    if (reposResult.status === 'fulfilled') {
      setRepositories(reposResult.value.items);
    } else if (status?.repository_info) {
      setRepositories([status.repository_info]);
      errors.push(
        'Showing the connected repository; the repository list could not be refreshed.',
      );
    } else {
      setRepositories([]);
      errors.push('Could not load connected repositories.');
    }
    setError(errors.join(' '));
    setLoading(false);
  }, [user]);
  useEffect(() => {
    void load();
    const params = new URLSearchParams(window.location.search);
    if (params.get('connected') === '1') {
      toast(
        'success',
        `GitHub connected${params.get('account') ? `: ${params.get('account')}` : ''}.`,
      );
      window.history.replaceState({}, '', '/repositories');
    } else if (params.has('error')) {
      toast(
        'error',
        'GitHub could not be connected. Check the app setup and try again.',
      );
      window.history.replaceState({}, '', '/repositories');
    }
  }, [load, toast]);
  async function connect() {
    setBusy(true);
    try {
      const { url } = await beginGitHubInstall(user);
      window.location.assign(url);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not start GitHub connection.',
      );
      setBusy(false);
    }
  }
  async function disconnect() {
    if (connection?.mode !== 'app') return;
    if (
      !window.confirm(
        'Disconnect this GitHub connection? Tasks and pull request records will remain.',
      )
    )
      return;
    setBusy(true);
    try {
      await disconnectGitHub(user);
      toast('success', 'GitHub disconnected.');
      await load();
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not disconnect GitHub.',
      );
    } finally {
      setBusy(false);
    }
  }
  async function connectWithPAT(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const [owner, repo, extra] = repositoryName.trim().split('/');
    if (!owner || !repo || extra) {
      toast('error', 'Enter the repository as owner/repository.');
      return;
    }
    setBusy(true);
    try {
      await connectGitHubPAT(user, { token: pat, owner, repo });
      setPat('');
      toast('success', `Connected ${owner}/${repo} with a fine-grained token.`);
      await load();
    } catch (e) {
      toast(
        'error',
        e instanceof Error
          ? e.message
          : 'Could not connect this GitHub repository.',
      );
    } finally {
      setBusy(false);
    }
  }
  async function removeConnectedRepository(
    repo: Awaited<ReturnType<typeof listRepositories>>['items'][number],
  ) {
    if (repo.mode !== 'pat') return;
    setBusy(true);
    try {
      const impact = await getRepositoryRemovalImpact(user, repo.key);
      const pendingWarning = impact.pending_task_count
        ? ` ${impact.pending_task_count} pending task(s) will be hidden while the repository is disconnected. They will reappear if you reconnect the same repository.`
        : '';
      if (
        !window.confirm(
          `Remove ${repo.label} from your connected repositories?${pendingWarning} Task records and history will not be deleted.`,
        )
      )
        return;
      await removeRepository(user, repo.key, impact.pending_task_count > 0);
      toast('success', `${repo.label} was removed. Task history was kept.`);
      await load();
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not remove this repository.',
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Heading
        eyebrow="INTEGRATIONS / SOURCE CONTROL"
        title="Repositories"
        description="Connect GitHub and choose the repositories the NEXUS coding workflow can access."
        action={
          <Button variant="secondary" onClick={() => void load()}>
            <RefreshCw size={16} />
            Refresh
          </Button>
        }
      />
      {error && (
        <div className="mb-5">
          <ErrorState message={error} retry={() => void load()} />
        </div>
      )}
      <Panel className="mb-6 flex flex-wrap items-center justify-between gap-5 p-5 sm:p-7">
        <div className="flex items-start gap-4">
          <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-elevated text-mint">
            <GitBranch size={22} />
          </span>
          <div>
            <p className="font-semibold">
              {connection?.connected
                ? `Connected to ${connection.account_login || 'GitHub'}${connection.repository ? ` · ${connection.repository}` : ''}`
                : 'GitHub is not connected'}
            </p>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
              {connection?.connected
                ? connection.mode === 'pat'
                  ? 'Your fine-grained token is stored in Google Secret Manager and is never returned to the browser. Repository access is scoped to your account.'
                  : 'Connected through a GitHub App installation. Repository access is controlled by the installation.'
                : 'Connect with a GitHub App or add repositories with fine-grained personal access tokens.'}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          {connection?.connected && connection.mode === 'app' ? (
            <Button
              variant="secondary"
              loading={busy}
              onClick={() => void disconnect()}
            >
              Disconnect
            </Button>
          ) : null}
        </div>
      </Panel>
      <Panel className="mb-6 p-5 sm:p-7">
        <h2 className="font-display text-lg font-semibold">
          Add a repository with a fine-grained token
        </h2>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
          Create a token limited to this repository with Contents and Pull
          requests set to Read and write. NEXUS verifies access and stores the
          token in Google Secret Manager; it is not saved to the browser or
          Firestore. Repositories and tasks are visible only to users who
          connect the same repository.
        </p>
        <form
          className="mt-5 grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end"
          onSubmit={(event) => void connectWithPAT(event)}
        >
          <label className="grid gap-2 text-sm font-medium text-zinc-200">
            Repository
            <input
              required
              autoComplete="off"
              placeholder="owner/repository"
              value={repositoryName}
              onChange={(event) => setRepositoryName(event.target.value)}
              className="min-h-11 rounded-xl border border-line bg-canvas px-3 text-sm outline-none focus:border-mint"
            />
          </label>
          <label className="grid gap-2 text-sm font-medium text-zinc-200">
            Fine-grained token
            <input
              required
              type="password"
              autoComplete="new-password"
              placeholder="github_pat_…"
              value={pat}
              onChange={(event) => setPat(event.target.value)}
              className="min-h-11 rounded-xl border border-line bg-canvas px-3 text-sm outline-none focus:border-mint"
            />
          </label>
          <Button type="submit" loading={busy}>
            <GitBranch size={16} />
            Add repository
          </Button>
        </form>
      </Panel>
      {connection?.configured && (
        <Panel className="mb-6 flex flex-wrap items-center justify-between gap-4 p-5">
          <div>
            <h2 className="font-semibold">Already created a GitHub App?</h2>
            <p className="mt-1 text-sm text-muted">
              This option requires a registered App and its backend credentials.
              For one repository, use the fine-grained token form above.
            </p>
          </div>
          <Button
            variant="secondary"
            loading={busy}
            onClick={() => void connect()}
          >
            <GitBranch size={16} />
            Install existing GitHub App
          </Button>
        </Panel>
      )}
      {loading ? (
        <LoadingState label="Loading connected repositories…" />
      ) : repositories.length ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {repositories.map((repo) => (
            <Panel key={repo.key} className="p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-semibold">{repo.label}</p>
                  <p className="mt-1 text-xs text-muted">
                    Base branch: {repo.base_branch || 'main'}
                  </p>
                </div>
                <div className="flex shrink-0 gap-1.5">
                  {repo.private && <Badge>Private</Badge>}
                  <Badge tone="success">Connected</Badge>
                </div>
              </div>
              <p className="mt-4 text-xs text-muted">
                Available for new tasks. Changes publish through a validated
                pull request.
              </p>
              {repo.owner && repo.repo && (
                <a
                  className="mt-4 inline-block text-xs font-semibold text-mint hover:underline"
                  href={`https://github.com/${repo.owner}/${repo.repo}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open on GitHub ↗
                </a>
              )}
              {repo.mode === 'pat' && (
                <Button
                  variant="secondary"
                  className="mt-4"
                  loading={busy}
                  onClick={() => void removeConnectedRepository(repo)}
                >
                  <Trash2 size={15} />
                  Remove from workspace
                </Button>
              )}
            </Panel>
          ))}
        </div>
      ) : (
        <EmptyState
          title={
            connection?.connected
              ? 'No repositories available'
              : 'Connect a GitHub account'
          }
          description={
            connection?.connected
              ? connection.mode === 'pat'
                ? 'The connected repository could not be loaded. Refresh this page to try again.'
                : 'Grant repository access to the installed NEXUS GitHub App, then refresh this page.'
              : 'Once connected, your permitted repositories will appear here and in the task form.'
          }
        />
      )}
    </>
  );
}

function TaskDetail() {
  const { taskId = '' } = useParams();
  const navigate = useNavigate();
  const { user, role } = useAuth();
  const toast = useToast();
  const [task, setTask] = useState<Task | null>(null);
  const [executions, setExecutions] = useState<Execution[]>([]);
  const [history, setHistory] = useState<Activity[]>([]);
  const [historyBefore, setHistoryBefore] = useState<string | null>(null);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [approvals, setApprovals] = useState<
    Awaited<ReturnType<typeof listApprovals>>
  >([]);
  const [validation, setValidation] =
    useState<Awaited<ReturnType<typeof getValidationReport>>>(null);
  const [security, setSecurity] =
    useState<Awaited<ReturnType<typeof getSecurityReview>>>(null);
  const [pr, setPr] =
    useState<Awaited<ReturnType<typeof getPullRequest>>>(null);
  const [patchPreview, setPatchPreview] = useState<ExecutionDiff | null>(null);
  const [patchPreviewError, setPatchPreviewError] = useState('');
  const [patchPreviewTarget, setPatchPreviewTarget] = useState('');
  const [patchPreviewLoading, setPatchPreviewLoading] = useState(false);
  const [eligibility, setEligibility] = useState<{
    eligible: boolean;
    reason: string | null;
  }>({ eligible: false, reason: null });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [modelChoices, setModelChoices] = useState<ModelChoices | null>(null);
  const [analysisModel, setAnalysisModel] = useState('gemini-3.8-flash');
  const [codingModel, setCodingModel] = useState('gemini-3.8-flash');
  const [editingPlan, setEditingPlan] = useState(false);
  const [planDraft, setPlanDraft] = useState<ExecutionPlanInput | null>(null);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [pendingMode, setPendingMode] = useState<Mode | null>(null);
  const [pendingRisk, setPendingRisk] = useState<Risk | null>(null);
  const [riskReason, setRiskReason] = useState('');
  const [humanAnswer, setHumanAnswer] = useState('');
  const [approvalReason, setApprovalReason] = useState('');
  const coreLoading = useRef(false);
  const slowRefreshLoading = useRef(false);
  const evidenceLoading = useRef(false);
  const slowRefreshAt = useRef(0);
  const evidenceRefreshAt = useRef(0);
  const evidenceExecutionId = useRef('');
  const historyLoadedOlder = useRef(false);
  const latestExecutionStatus = executions.at(-1)?.status;
  const executionActive =
    ['QUEUED', 'RUNNING'].includes(latestExecutionStatus || '') ||
    ['IN_PROGRESS', 'VALIDATING', 'SECURITY_REVIEW'].includes(
      task?.status || '',
    );
  useEffect(() => {
    let active = true;
    void getModelChoices(user)
      .then((choices) => {
        if (!active) return;
        setModelChoices(choices);
        setAnalysisModel(choices.analysis_default);
        setCodingModel(choices.coding_default);
      })
      .catch(() => {
        // Keep the recommended defaults usable while the model API is unavailable.
      });
    return () => {
      active = false;
    };
  }, [user]);
  const load = useCallback(
    async (forceDetails = false) => {
      if (coreLoading.current) return;
      coreLoading.current = true;
      try {
        const [t, ex] = await Promise.all([
          getTask(user, taskId),
          listTaskExecutions(user, taskId),
        ]);
        setTask(t);
        setExecutions(ex);
        setError('');
        setLoading(false);
        const latest = ex.at(-1);
        if (latest) {
          if (
            !evidenceLoading.current &&
            (forceDetails ||
              latest.id !== evidenceExecutionId.current ||
              Date.now() - evidenceRefreshAt.current >= 10_000)
          ) {
            evidenceLoading.current = true;
            evidenceExecutionId.current = latest.id;
            void Promise.all([
              getValidationReport(user, latest.id),
              getSecurityReview(user, latest.id),
              getPullRequest(user, latest.id),
              getPullRequestEligibility(user, latest.id),
            ])
              .then(([v, s, p, e]) => {
                setValidation(v);
                setSecurity(s);
                setPr(p);
                setEligibility(e);
                evidenceRefreshAt.current = Date.now();
              })
              .catch((e: unknown) => {
                setError(
                  e instanceof Error
                    ? e.message
                    : 'Could not refresh execution evidence.',
                );
              })
              .finally(() => {
                evidenceLoading.current = false;
              });
          }
        } else {
          setValidation(null);
          setSecurity(null);
          setPr(null);
          setEligibility({ eligible: false, reason: null });
          evidenceExecutionId.current = '';
        }
        if (
          !slowRefreshLoading.current &&
          (forceDetails || Date.now() - slowRefreshAt.current >= 30_000)
        ) {
          slowRefreshLoading.current = true;
          void Promise.all([
            listTaskHistory(user, taskId),
            listApprovals(user, taskId),
          ])
            .then(([page, decisions]) => {
              setHistory((previous) =>
                dedupeActivity([
                  ...previous,
                  ...page.items,
                  ...taskActivity(t),
                ]),
              );
              if (!historyLoadedOlder.current) {
                setHistoryBefore(page.items.at(-1)?.occurred_at ?? null);
              }
              setHistoryHasMore(page.has_more);
              setApprovals(decisions);
              slowRefreshAt.current = Date.now();
            })
            .catch((e: unknown) => {
              setError(
                e instanceof Error
                  ? e.message
                  : 'Could not refresh task activity.',
              );
            })
            .finally(() => {
              slowRefreshLoading.current = false;
            });
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not load task.');
      } finally {
        coreLoading.current = false;
      }
    },
    [user, taskId],
  );
  useEffect(() => {
    void load();
    const timer = window.setInterval(
      () => {
        if (document.visibilityState === 'visible') void load();
      },
      executionActive ? 5_000 : 12_000,
    );
    return () => window.clearInterval(timer);
  }, [load, executionActive]);
  async function loadOlderHistory() {
    if (!historyBefore || historyLoadingMore) return;
    setHistoryLoadingMore(true);
    try {
      const page = await listTaskHistory(user, taskId, historyBefore);
      setHistory((previous) => dedupeActivity([...previous, ...page.items]));
      setHistoryBefore(page.items.at(-1)?.occurred_at ?? null);
      setHistoryHasMore(page.has_more);
      historyLoadedOlder.current = true;
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not load older activity.',
      );
    } finally {
      setHistoryLoadingMore(false);
    }
  }
  async function showPatch(executionId: string) {
    setPatchPreviewTarget(executionId);
    setPatchPreviewLoading(true);
    setPatchPreviewError('');
    try {
      setPatchPreview(await getExecutionDiff(user, executionId));
    } catch (e) {
      setPatchPreview(null);
      setPatchPreviewError(
        e instanceof Error ? e.message : 'Could not load the patch preview.',
      );
    } finally {
      setPatchPreviewLoading(false);
    }
  }
  async function action(
    actionName: 'analyze' | 'execute' | 'retry' | 'stop' | 'pr' | 'sync-pr',
  ) {
    if (!task) return;
    setBusy(true);
    try {
      if (actionName === 'analyze')
        await analyzeTask(user, task.id, analysisModel);
      else if (actionName === 'execute')
        await startExecution(user, task.id, codingModel);
      else if (actionName === 'retry')
        await retryExecution(user, task.id, codingModel);
      else if (actionName === 'stop' && executions.at(-1))
        await stopExecution(user, task.id, executions.at(-1)!.id);
      else if (actionName === 'pr' && executions.at(-1))
        await createPullRequest(user, task.id, executions.at(-1)!.id);
      else if (actionName === 'sync-pr' && executions.at(-1))
        await syncPullRequest(user, task.id, executions.at(-1)!.id);
      toast(
        'success',
        actionName === 'analyze'
          ? 'Analysis request completed.'
          : actionName === 'execute'
            ? 'Execution attempt recorded.'
            : actionName === 'retry'
              ? 'Execution retry queued.'
              : actionName === 'stop'
                ? 'Execution stopped and its workspace cleaned up.'
                : actionName === 'sync-pr'
                  ? 'GitHub pull request status synchronized.'
                  : 'Pull request request recorded.',
      );
      await load(true);
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Action failed.');
      await load(true);
    } finally {
      setBusy(false);
    }
  }
  async function submitApproval(status: 'APPROVED' | 'REJECTED') {
    if (!task || !latest || !approvalReason.trim()) {
      toast('error', 'Add a decision reason before approving or rejecting.');
      return;
    }
    setBusy(true);
    try {
      await decideApproval(user, task.id, latest.id, {
        status,
        reason: approvalReason.trim(),
      });
      setApprovalReason('');
      toast(
        'success',
        status === 'APPROVED'
          ? 'Approval recorded. PR publication will continue.'
          : 'Rejection recorded for this patch.',
      );
      await load(true);
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Decision failed.');
      await load(true);
    } finally {
      setBusy(false);
    }
  }
  async function submitHumanAnswer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!task || !humanAnswer.trim()) return;
    setBusy(true);
    try {
      const continuation = await answerWorkflowQuestion(
        user,
        task.id,
        humanAnswer.trim(),
      );
      setHumanAnswer('');
      const resumedSameExecution =
        task.pending_execution_id &&
        'task_id' in continuation &&
        continuation.id === task.pending_execution_id;
      const message = !task.pending_execution_id
        ? 'Guidance saved. The planner is analyzing the task again.'
        : resumedSameExecution
          ? 'Guidance saved. The coding workflow is resuming in its existing workspace.'
          : 'Guidance saved. A fresh execution is starting with the saved guidance.';
      toast('success', message);
      await load(true);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not submit guidance.',
      );
      await load(true);
    } finally {
      setBusy(false);
    }
  }
  function beginPlanEdit() {
    if (!task?.execution_plan) return;
    const plan = task.execution_plan;
    setPlanDraft({
      risk: plan.risk,
      complexity: plan.complexity,
      mode: plan.mode,
      approval_required: plan.approval_required,
      reason: plan.reason,
      assigned_agents: [...plan.assigned_agents],
      steps: [...plan.steps],
      expected_files_or_areas: [...plan.expected_files_or_areas],
      validation_plan: [...plan.validation_plan],
      potential_concerns: [...plan.potential_concerns],
    });
    setEditingPlan(true);
  }
  async function savePlan(reanalyze = false) {
    if (!task || !planDraft) return;
    setBusy(true);
    try {
      const updated = await updateTaskPlan(user, task.id, planDraft);
      setTask(updated);
      setEditingPlan(false);
      if (reanalyze) {
        const analyzed = await analyzeTask(user, task.id, analysisModel);
        setTask(analyzed);
        toast('success', 'Plan updated and re-analyzed.');
      } else {
        toast('success', 'Plan updated.');
      }
      await load(true);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not update the plan.',
      );
    } finally {
      setBusy(false);
    }
  }
  async function cloneEditedPlan() {
    if (!task || !planDraft) return;
    setBusy(true);
    try {
      const cloned = await cloneTaskPlan(user, task.id, {
        ...planDraft,
        title: `${task.title} (plan copy)`.slice(0, 200),
      });
      toast('success', 'A separate task was created from the edited plan.');
      navigate(`/tasks/${cloned.id}`);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not clone the plan.',
      );
    } finally {
      setBusy(false);
    }
  }
  async function saveTaskPolicy() {
    if (!task || !task.mode || !task.risk) return;
    const mode = pendingMode ?? task.mode;
    const risk = pendingRisk ?? task.risk;
    const riskChanged = risk !== task.risk;
    if (mode === task.mode && !riskChanged) return;
    if (riskChanged && !riskReason.trim()) {
      toast('error', 'Add a reason for changing the risk assessment.');
      return;
    }
    setBusy(true);
    try {
      await setTaskMode(
        user,
        task.id,
        mode,
        riskChanged ? risk : undefined,
        riskChanged ? riskReason.trim() : undefined,
      );
      setPendingMode(null);
      setPendingRisk(null);
      setRiskReason('');
      toast('success', 'Task risk and execution mode updated.');
      await load(true);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Could not update task policy.',
      );
    } finally {
      setBusy(false);
    }
  }
  async function addAttachments(files: File[]) {
    if (!task || !files.length) return;
    setAttachmentBusy(true);
    try {
      await uploadTaskAttachments(user, task.id, files);
      toast('success', 'Attachments saved for the coding harness.');
      await load(true);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Attachments could not be uploaded.',
      );
    } finally {
      setAttachmentBusy(false);
    }
  }
  async function removeAttachment(attachmentId: string) {
    if (!task || !window.confirm('Remove this task attachment?')) return;
    setAttachmentBusy(true);
    try {
      await deleteTaskAttachment(user, task.id, attachmentId);
      await load(true);
      toast('success', 'Attachment removed.');
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Attachment could not be removed.',
      );
    } finally {
      setAttachmentBusy(false);
    }
  }
  async function saveAttachment(attachmentId: string, filename: string) {
    if (!task) return;
    try {
      await downloadTaskAttachment(user, task.id, attachmentId, filename);
    } catch (e) {
      toast(
        'error',
        e instanceof Error ? e.message : 'Attachment download failed.',
      );
    }
  }
  if (loading) return <LoadingState label="Loading task evidence…" />;
  if (error || !task)
    return (
      <ErrorState
        message={error || 'Task not found.'}
        retry={() => void load()}
      />
    );
  const latest = executions.at(-1);
  const previousExecutions = latest
    ? executions.filter((execution) => execution.id !== latest.id).reverse()
    : [];
  const executionEvents = latest
    ? history
        .filter((event) => event.execution_id === latest.id)
        .sort((a, b) => Date.parse(a.occurred_at) - Date.parse(b.occurred_at))
    : [];
  const executionIsLive = ['QUEUED', 'RUNNING'].includes(latest?.status || '');
  const executionNeedsRecovery =
    ['IN_PROGRESS', 'VALIDATING', 'SECURITY_REVIEW'].includes(task.status) &&
    latest?.status === 'CANCELLED';
  const staleValidation = Boolean(
    validation &&
    latest?.patch_fingerprint &&
    validation.patch_fingerprint !== latest.patch_fingerprint,
  );
  const staleSecurity = Boolean(
    security &&
    latest?.patch_fingerprint &&
    security.patch_fingerprint !== latest.patch_fingerprint,
  );
  const planListField = (
    title: string,
    field:
      | 'assigned_agents'
      | 'steps'
      | 'expected_files_or_areas'
      | 'validation_plan'
      | 'potential_concerns',
  ) => (
    <label className="grid gap-1 text-xs font-semibold text-muted">
      {title}
      <textarea
        className="min-h-24 rounded-xl border border-line bg-canvas p-3 text-sm font-normal text-zinc-100 outline-none focus:border-mint"
        value={planDraft?.[field].join('\n') || ''}
        onChange={(event) =>
          setPlanDraft((current) =>
            current
              ? {
                  ...current,
                  [field]: event.target.value
                    .split('\n')
                    .map((item) => item.trim())
                    .filter(Boolean),
                }
              : current,
          )
        }
        placeholder="One item per line"
      />
    </label>
  );
  const availableModels = modelChoices?.models ?? [
    {
      id: 'gemini-3.8-flash',
      label: 'Gemini 3.8 Flash (recommended)',
      description: 'Recommended model for analysis and coding tasks.',
    },
  ];
  return (
    <>
      <Link to="/tasks" className="mb-5 inline-block text-sm text-mint">
        ← All tasks
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="eyebrow">
            {task.repository_label || task.repository_key} /{' '}
            {label(task.task_type)}
          </p>
          <h1 className="mt-2 font-display text-3xl font-bold">{task.title}</h1>
          <div className="mt-3 flex flex-wrap gap-2">
            <Badge tone={statusTone(task.status)}>{label(task.status)}</Badge>
            <Badge tone={modeTone(task.mode)}>{label(task.mode)}</Badge>
            <Badge
              tone={
                task.risk === 'HIGH' || task.risk === 'CRITICAL'
                  ? 'danger'
                  : task.risk
                    ? 'warning'
                    : 'neutral'
              }
            >
              {label(task.risk)}
            </Badge>
          </div>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          {!task.pending_question &&
            ['BACKLOG', 'READY', 'HUMAN_QUEUE', 'FAILED', 'BLOCKED'].includes(
              task.status,
            ) && (
              <label className="grid gap-1 text-xs font-semibold text-muted">
                Analysis model
                <select
                  aria-label="Analysis model"
                  className="min-h-10 rounded-xl border border-line bg-panel px-3 text-sm text-zinc-100"
                  value={analysisModel}
                  disabled={busy || role === 'viewer'}
                  onChange={(event) => setAnalysisModel(event.target.value)}
                >
                  {availableModels.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.label}
                      {model.id === modelChoices?.analysis_default
                        ? ' · recommended'
                        : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}
          {(task.status === 'READY' ||
            (['FAILED', 'BLOCKED'].includes(task.status) &&
              task.execution_plan)) && (
            <label className="grid gap-1 text-xs font-semibold text-muted">
              Coding model
              <select
                aria-label="Coding model"
                className="min-h-10 rounded-xl border border-line bg-panel px-3 text-sm text-zinc-100"
                value={codingModel}
                disabled={busy || role === 'viewer'}
                onChange={(event) => setCodingModel(event.target.value)}
              >
                {availableModels.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.label}
                    {model.id === modelChoices?.coding_default
                      ? ' · recommended'
                      : ''}
                  </option>
                ))}
              </select>
            </label>
          )}
          {!task.pending_question &&
            ['BACKLOG', 'READY', 'HUMAN_QUEUE', 'FAILED', 'BLOCKED'].includes(
              task.status,
            ) && (
              <Button loading={busy} onClick={() => void action('analyze')}>
                <Sparkles size={16} />
                {task.execution_plan ? 'Re-analyze' : 'Analyze'}
              </Button>
            )}
          {task.status === 'READY' && (
            <Button loading={busy} onClick={() => void action('execute')}>
              <Zap size={16} />
              Start execution
            </Button>
          )}
          {executionIsLive && role !== 'viewer' && (
            <Button
              loading={busy}
              variant="secondary"
              onClick={() => void action('stop')}
            >
              <Square size={15} />
              Stop execution
            </Button>
          )}
          {(['FAILED', 'BLOCKED'].includes(task.status) ||
            executionNeedsRecovery) &&
            !task.pending_question &&
            task.execution_plan &&
            ['AI', 'HYBRID'].includes(task.mode || '') && (
              <Button loading={busy} onClick={() => void action('retry')}>
                <Zap size={16} />
                {executionNeedsRecovery
                  ? 'Resume cancelled execution'
                  : 'Retry execution'}
              </Button>
            )}
          {eligibility.eligible && (
            <Button loading={busy} onClick={() => void action('pr')}>
              <GitPullRequest size={16} />
              Create pull request
            </Button>
          )}
        </div>
      </div>
      {task.status === 'AWAITING_APPROVAL' && (
        <Panel className="mt-5 border border-amber-400/30 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-display text-lg font-semibold">
                Approval required before PR publication
              </h2>
              <p className="mt-1 text-sm text-muted">
                Review the validation and security evidence below, then record a
                decision for this exact patch.
              </p>
            </div>
            <Badge tone="warning">Awaiting approval</Badge>
          </div>
          {latest?.patch_fingerprint && (
            <p className="mt-3 text-xs text-muted">
              Patch <code>{latest.patch_fingerprint.slice(0, 16)}</code> ·{' '}
              {latest.changed_files.length} changed files
            </p>
          )}
          {role === 'admin' ? (
            <>
              <label className="mt-4 grid gap-2 text-sm font-medium text-zinc-200">
                Decision reason
                <textarea
                  aria-label="Approval decision reason"
                  className="min-h-20 rounded-xl border border-line bg-canvas p-3 text-sm font-normal outline-none focus:border-mint"
                  maxLength={2000}
                  value={approvalReason}
                  onChange={(event) => setApprovalReason(event.target.value)}
                  placeholder="Explain why this patch is approved or rejected."
                />
              </label>
              <div className="mt-3 flex flex-wrap justify-end gap-2">
                <Button
                  variant="secondary"
                  loading={busy}
                  disabled={!latest || !approvalReason.trim()}
                  onClick={() => void submitApproval('REJECTED')}
                >
                  Reject patch
                </Button>
                <Button
                  loading={busy}
                  disabled={!latest || !approvalReason.trim()}
                  onClick={() => void submitApproval('APPROVED')}
                >
                  <CheckCircle2 size={16} /> Approve and publish PR
                </Button>
              </div>
            </>
          ) : (
            <p className="mt-3 text-sm text-muted">
              An administrator must record the decision. The approval is also
              available from the Approvals tab.
            </p>
          )}
        </Panel>
      )}
      {['AWAITING_INPUT', 'BLOCKED'].includes(task.status) &&
        task.pending_question && (
          <Panel className="mt-5 border border-amber-400/30 p-5">
            <h2 className="font-display text-lg font-semibold">
              Operator input needed
            </h2>
            <p className="mt-2 text-sm leading-6 text-muted">
              {task.pending_question}
            </p>
            {role !== 'viewer' && (
              <form
                className="mt-4 grid gap-3"
                onSubmit={(event) => void submitHumanAnswer(event)}
              >
                <label className="grid gap-2 text-sm font-medium text-zinc-200">
                  Guidance for the next attempt
                  <textarea
                    required
                    maxLength={4000}
                    value={humanAnswer}
                    onChange={(event) => setHumanAnswer(event.target.value)}
                    className="min-h-28 rounded-xl border border-line bg-canvas p-3 text-sm font-normal outline-none focus:border-mint"
                    placeholder="Explain what should change or what the agent should inspect."
                  />
                </label>
                <div>
                  <Button loading={busy} disabled={!humanAnswer.trim()}>
                    Continue workflow
                  </Button>
                </div>
              </form>
            )}
          </Panel>
        )}
      {['READY', 'HUMAN_QUEUE'].includes(task.status) && role !== 'viewer' && (
        <Panel className="mt-5 p-5">
          <div className="grid gap-5 lg:grid-cols-[1fr_auto]">
            <div>
              <h2 className="font-display text-lg font-semibold">
                Review task policy
              </h2>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
                Choose the risk assessment and work mode before execution
                starts. Your selections override the analysis recommendation and
                task-type defaults. AI still requires coder and validator
                permissions; configured approval and security-review checks
                remain in effect.
              </p>
            </div>
            <div className="grid gap-4 sm:grid-cols-2 lg:min-w-[460px]">
              <label className="grid gap-2 text-sm font-medium text-zinc-200">
                Risk assessment
                <select
                  value={pendingRisk ?? task.risk ?? 'MEDIUM'}
                  onChange={(event) =>
                    setPendingRisk(event.target.value as Risk)
                  }
                  className="min-h-11 rounded-xl border border-line bg-canvas px-3 text-sm outline-none focus:border-mint"
                >
                  {(['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as const).map(
                    (risk) => (
                      <option key={risk} value={risk}>
                        {label(risk)}
                      </option>
                    ),
                  )}
                </select>
              </label>
              <div className="grid gap-2 text-sm font-medium text-zinc-200">
                Execution mode
                <div
                  className="flex flex-wrap gap-2"
                  role="group"
                  aria-label="Task execution mode"
                >
                  {(['HUMAN', 'HYBRID', 'AI'] as const).map((mode) => (
                    <Button
                      key={mode}
                      variant={
                        (pendingMode ?? task.mode) === mode
                          ? 'primary'
                          : 'secondary'
                      }
                      disabled={busy}
                      onClick={() => setPendingMode(mode)}
                    >
                      {label(mode)}
                    </Button>
                  ))}
                </div>
              </div>
              {(pendingRisk ?? task.risk) !== task.risk && (
                <label className="grid gap-2 text-sm font-medium text-zinc-200 sm:col-span-2">
                  Reason for changing risk
                  <textarea
                    required
                    maxLength={1000}
                    value={riskReason}
                    onChange={(event) => setRiskReason(event.target.value)}
                    placeholder="Explain the evidence behind this risk reassessment."
                    className="min-h-20 rounded-xl border border-line bg-canvas px-3 py-2 text-sm outline-none focus:border-mint"
                  />
                </label>
              )}
              <div className="flex items-center justify-between gap-3 sm:col-span-2">
                <p className="text-xs text-muted">
                  Changes are recorded in task activity.
                </p>
                <Button
                  loading={busy}
                  disabled={
                    (pendingMode ?? task.mode) === task.mode &&
                    (pendingRisk ?? task.risk) === task.risk
                  }
                  onClick={() => void saveTaskPolicy()}
                >
                  Apply changes
                </Button>
              </div>
            </div>
          </div>
        </Panel>
      )}
      <div className="mt-6 grid gap-5 xl:grid-cols-[1fr_340px]">
        <div className="grid content-start gap-5">
          <Panel className="p-5">
            <h2 className="font-display text-lg font-semibold">Task brief</h2>
            <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-zinc-300">
              {task.description}
            </p>
            <p className="mt-4 text-xs text-muted">
              Created by {task.created_by} · {fullDate(task.created_at)}
            </p>
          </Panel>
          <Panel className="p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Paperclip size={17} className="text-mint" />
                <h2 className="font-display text-lg font-semibold">
                  Task attachments
                </h2>
                <Badge>{task.attachments?.length || 0}</Badge>
              </div>
              {role !== 'viewer' && (task.attachments?.length || 0) < 10 && (
                <label className="inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-xl border border-line bg-elevated px-4 text-sm font-semibold hover:border-zinc-500">
                  <Paperclip size={15} />
                  Add files
                  <input
                    className="sr-only"
                    type="file"
                    multiple
                    accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.txt,.md,.csv,.json,application/pdf,image/*,text/plain,text/markdown,text/csv,application/json"
                    disabled={attachmentBusy}
                    onChange={(event) => {
                      void addAttachments(Array.from(event.target.files ?? []));
                      event.target.value = '';
                    }}
                  />
                </label>
              )}
            </div>
            <p className="mt-2 text-xs leading-5 text-muted">
              PDFs, images, text, Markdown, CSV, and JSON. The Antigravity
              coding harness receives these as untrusted task context.
            </p>
            {task.attachments?.length ? (
              <div className="mt-4 divide-y divide-line">
                {task.attachments.map((attachment) => (
                  <div
                    key={attachment.id}
                    className="flex items-center gap-3 py-3 first:pt-0 last:pb-0"
                  >
                    <Paperclip size={15} className="shrink-0 text-muted" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">
                        {attachment.filename}
                      </p>
                      <p className="text-xs text-muted">
                        {(attachment.size_bytes / 1024).toFixed(0)} KB ·{' '}
                        {attachment.content_type}
                      </p>
                    </div>
                    <button
                      aria-label={`Download ${attachment.filename}`}
                      className="rounded-lg p-2 text-muted hover:bg-elevated hover:text-mint"
                      onClick={() =>
                        void saveAttachment(attachment.id, attachment.filename)
                      }
                    >
                      <Download size={16} />
                    </button>
                    {role !== 'viewer' && (
                      <button
                        aria-label={`Remove ${attachment.filename}`}
                        disabled={attachmentBusy}
                        className="rounded-lg p-2 text-muted hover:bg-elevated hover:text-rose-300"
                        onClick={() => void removeAttachment(attachment.id)}
                      >
                        <Trash2 size={16} />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-4 text-sm text-muted">No attachments added.</p>
            )}
          </Panel>
          {task.execution_plan && (
            <Panel className="p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Sparkles size={17} className="text-mint" />
                  <h2 className="font-display text-lg font-semibold">
                    Analysis plan
                  </h2>
                </div>
                {role !== 'viewer' &&
                  [
                    'BACKLOG',
                    'READY',
                    'HUMAN_QUEUE',
                    'FAILED',
                    'BLOCKED',
                  ].includes(task.status) && (
                    <Button
                      variant="secondary"
                      onClick={() =>
                        editingPlan
                          ? (setEditingPlan(false), setPlanDraft(null))
                          : beginPlanEdit()
                      }
                    >
                      {editingPlan ? (
                        <CircleAlert size={15} />
                      ) : (
                        <Pencil size={15} />
                      )}
                      {editingPlan ? 'Cancel edits' : 'Edit plan'}
                    </Button>
                  )}
              </div>
              {editingPlan && planDraft && (
                <div className="mt-5 grid gap-4 border-b border-line pb-5">
                  <label className="grid gap-1 text-xs font-semibold text-muted">
                    Plan rationale
                    <textarea
                      className="min-h-24 rounded-xl border border-line bg-canvas p-3 text-sm font-normal text-zinc-100 outline-none focus:border-mint"
                      value={planDraft.reason}
                      onChange={(event) =>
                        setPlanDraft({
                          ...planDraft,
                          reason: event.target.value,
                        })
                      }
                    />
                  </label>
                  <div className="grid gap-3 sm:grid-cols-3">
                    <label className="grid gap-1 text-xs font-semibold text-muted">
                      Risk
                      <select
                        className="rounded-xl border border-line bg-canvas p-3 text-sm text-zinc-100"
                        value={planDraft.risk}
                        onChange={(event) =>
                          setPlanDraft({
                            ...planDraft,
                            risk: event.target.value as Risk,
                          })
                        }
                      >
                        {['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                    <label className="grid gap-1 text-xs font-semibold text-muted">
                      Complexity
                      <select
                        className="rounded-xl border border-line bg-canvas p-3 text-sm text-zinc-100"
                        value={planDraft.complexity}
                        onChange={(event) =>
                          setPlanDraft({
                            ...planDraft,
                            complexity: event.target
                              .value as ExecutionPlanInput['complexity'],
                          })
                        }
                      >
                        {['LOW', 'MEDIUM', 'HIGH'].map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                    <label className="grid gap-1 text-xs font-semibold text-muted">
                      Execution mode
                      <select
                        className="rounded-xl border border-line bg-canvas p-3 text-sm text-zinc-100"
                        value={planDraft.mode}
                        onChange={(event) =>
                          setPlanDraft({
                            ...planDraft,
                            mode: event.target.value as Mode,
                          })
                        }
                      >
                        {['HUMAN', 'HYBRID', 'AI'].map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <label className="flex items-center gap-2 text-sm text-zinc-200">
                    <input
                      type="checkbox"
                      checked={planDraft.approval_required}
                      onChange={(event) =>
                        setPlanDraft({
                          ...planDraft,
                          approval_required: event.target.checked,
                        })
                      }
                    />
                    Require human approval before publishing
                  </label>
                  {planListField(
                    'Assigned agents (one per line)',
                    'assigned_agents',
                  )}
                  {planListField('Implementation steps', 'steps')}
                  {planListField(
                    'Expected files or areas',
                    'expected_files_or_areas',
                  )}
                  {planListField('Validation plan', 'validation_plan')}
                  {planListField('Potential concerns', 'potential_concerns')}
                  <div className="flex flex-wrap gap-2">
                    <Button loading={busy} onClick={() => void savePlan(false)}>
                      Save plan
                    </Button>
                    <Button
                      loading={busy}
                      variant="secondary"
                      onClick={() => void savePlan(true)}
                    >
                      <Sparkles size={15} /> Save and re-analyze
                    </Button>
                    <Button
                      loading={busy}
                      variant="secondary"
                      onClick={() => void cloneEditedPlan()}
                    >
                      <Copy size={15} /> Clone as new task
                    </Button>
                  </div>
                  <p className="text-xs text-muted">
                    Re-analysis uses this edited plan as input. Cloning creates
                    a separate task; the original task stays unchanged.
                  </p>
                </div>
              )}
              <p className="mt-3 text-sm leading-6 text-zinc-300">
                {task.execution_plan.reason}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Badge>{label(task.execution_plan.risk)} risk</Badge>
                <Badge>
                  {label(task.execution_plan.complexity)} complexity
                </Badge>
                <Badge>{label(task.execution_plan.mode)} mode</Badge>
                <Badge>
                  {task.execution_plan.approval_required
                    ? 'Human approval required'
                    : 'No human approval required'}
                </Badge>
                <Badge>
                  {task.execution_plan.security_review_required
                    ? 'Security review required'
                    : 'Security review not required'}
                </Badge>
                {task.execution_plan.operator_override && (
                  <Badge>Operator override</Badge>
                )}
              </div>
              {task.execution_plan.policy_reason && (
                <p className="mt-3 text-xs leading-5 text-muted">
                  Policy decision: {task.execution_plan.policy_reason}
                </p>
              )}
              <p className="mt-2 text-xs text-muted">
                Plan source: {label(task.execution_plan.source)}
                {task.execution_plan.model_id
                  ? ` · ${task.execution_plan.model_id}`
                  : ''}
              </p>
              <ol className="mt-4 grid gap-2">
                {task.execution_plan.steps.map((step, i) => (
                  <li key={`${i}-${step}`} className="flex gap-3 text-sm">
                    <span className="text-mint">
                      {String(i + 1).padStart(2, '0')}
                    </span>
                    <span>{step}</span>
                  </li>
                ))}
              </ol>
              <div className="mt-4 flex flex-wrap gap-2">
                {task.execution_plan.assigned_agents.map((agent) => (
                  <Badge key={agent}>{label(agent)}</Badge>
                ))}
              </div>
              {task.execution_plan.expected_files_or_areas.length > 0 && (
                <div className="mt-4">
                  <p className="text-xs font-semibold text-muted">
                    EXPECTED FILES OR AREAS
                  </p>
                  <ul className="mt-2 grid gap-1 text-sm text-zinc-300">
                    {task.execution_plan.expected_files_or_areas.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
              {task.execution_plan.validation_plan.length > 0 && (
                <div className="mt-4">
                  <p className="text-xs font-semibold text-muted">
                    PLANNED VALIDATION
                  </p>
                  <ul className="mt-2 grid gap-1 text-sm text-zinc-300">
                    {task.execution_plan.validation_plan.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
              {task.execution_plan.potential_concerns.length > 0 && (
                <div className="mt-4 rounded-lg border border-amber-800/60 bg-amber-950/20 p-3">
                  <p className="text-xs font-semibold text-amber-200">
                    PLAN CONCERNS
                  </p>
                  <ul className="mt-2 grid gap-1 text-sm text-amber-100/80">
                    {task.execution_plan.potential_concerns.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
            </Panel>
          )}
          <Panel className="p-5">
            <h2 className="font-display text-lg font-semibold">
              Execution evidence
            </h2>
            {latest ? (
              <div className="mt-4 grid gap-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={statusTone(latest.status)}>
                    {label(latest.status)}
                  </Badge>
                  <span className="text-xs text-muted">
                    Started {fullDate(latest.started_at || latest.created_at)}
                  </span>
                </div>
                {(latest.branch_reference || latest.base_commit_sha) && (
                  <div className="rounded-xl border border-line bg-elevated/40 p-4 text-sm">
                    <p className="font-semibold">Workspace and patch</p>
                    {latest.branch_reference && (
                      <p className="mt-2 break-all text-zinc-300">
                        Branch: <code>{latest.branch_reference}</code>
                      </p>
                    )}
                    {latest.base_commit_sha && (
                      <p className="mt-1 break-all text-xs text-muted">
                        Base commit: <code>{latest.base_commit_sha}</code>
                      </p>
                    )}
                    {latest.patch_fingerprint && (
                      <p className="mt-1 break-all text-xs text-muted">
                        Patch fingerprint:{' '}
                        <code>{latest.patch_fingerprint}</code>
                      </p>
                    )}
                    <p className="mt-2 text-xs text-muted">
                      {pr?.commit_sha
                        ? `Committed as ${pr.commit_sha} (${label(pr.status)}).`
                        : 'The validated changes are local and uncommitted. NEXUS creates the commit when GitHub publication starts after approval gates pass.'}
                    </p>
                  </div>
                )}
                {latest.patch_fingerprint && (
                  <div>
                    <button
                      className="rounded-lg border border-line px-3 py-2 text-xs font-semibold text-mint hover:bg-elevated disabled:opacity-60"
                      disabled={patchPreviewLoading}
                      onClick={() => void showPatch(latest.id)}
                      type="button"
                    >
                      {patchPreviewLoading
                        ? 'Loading patch…'
                        : 'View code patch'}
                    </button>
                    {patchPreviewError && patchPreviewTarget === latest.id && (
                      <p className="mt-2 text-xs text-rose-200">
                        Patch preview unavailable: {patchPreviewError}
                      </p>
                    )}
                    {patchPreview?.execution_id === latest.id && (
                      <details className="mt-3 rounded-xl border border-line bg-elevated/30 p-4">
                        <summary className="cursor-pointer text-sm font-semibold">
                          Patch preview · {patchPreview.changed_files.length}{' '}
                          files ·{' '}
                          {patchPreview.committed ? 'committed' : 'uncommitted'}
                        </summary>
                        <p className="mt-2 break-all text-[11px] text-muted">
                          {patchPreview.redacted
                            ? 'Credential patterns are redacted in this preview. '
                            : ''}
                          Fingerprint: {patchPreview.patch_fingerprint}
                        </p>
                        {patchPreview.commit_sha && (
                          <p className="mt-1 break-all text-[11px] text-muted">
                            Commit: <code>{patchPreview.commit_sha}</code>
                          </p>
                        )}
                        <pre className="mt-3 max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/30 p-3 font-mono text-[11px] leading-5 text-zinc-200">
                          {patchPreview.diff || 'No textual diff is available.'}
                        </pre>
                      </details>
                    )}
                  </div>
                )}
                {latest.error_details &&
                ['FAILED', 'BLOCKED', 'CANCELLED'].includes(latest.status) ? (
                  <div className="rounded-xl border border-rose-800/70 bg-rose-950/30 p-4">
                    <p className="text-sm font-semibold text-rose-200">
                      Execution failed
                    </p>
                    <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-rose-100/90">
                      {latest.error_details}
                    </p>
                  </div>
                ) : (
                  <p className="text-sm text-muted">
                    {latest.patch_summary ||
                      latest.final_result ||
                      'Waiting for execution evidence.'}
                  </p>
                )}
                {latest.patch_summary &&
                  latest.error_details &&
                  ['FAILED', 'BLOCKED', 'CANCELLED'].includes(
                    latest.status,
                  ) && (
                    <p className="text-sm text-muted">{latest.patch_summary}</p>
                  )}
                {latest.changed_files.length > 0 && (
                  <div>
                    <p className="mb-2 text-xs font-semibold text-muted">
                      CHANGED FILES
                    </p>
                    {latest.changed_files.map((file) => (
                      <code
                        key={file}
                        className="mr-2 inline-block rounded bg-elevated px-2 py-1 text-xs"
                      >
                        {file}
                      </code>
                    ))}
                  </div>
                )}
                <div className="grid gap-3 md:grid-cols-2">
                  {validation && (
                    <div
                      className={`rounded-xl border p-4 ${staleValidation ? 'border-amber-800 bg-amber-950/20' : 'border-line bg-elevated/50'}`}
                    >
                      <div className="flex justify-between">
                        <span className="font-semibold">Validation</span>
                        <Badge tone={statusTone(validation.overall_status)}>
                          {staleValidation
                            ? 'Stale'
                            : label(validation.overall_status)}
                        </Badge>
                      </div>
                      <p className="mt-2 text-xs leading-5 text-muted">
                        {validation.summary}
                      </p>
                      {validation.commands.map((cmd) => (
                        <div
                          className="mt-3 rounded-lg border border-line p-3"
                          key={cmd.command_id}
                        >
                          <div className="flex flex-wrap justify-between gap-2 text-xs">
                            <span>{label(cmd.command_id)}</span>
                            <span>
                              {label(cmd.status)}
                              {cmd.exit_code !== null
                                ? ` · exit ${cmd.exit_code}`
                                : ''}
                            </span>
                          </div>
                          {cmd.duration_ms !== null && (
                            <p className="mt-1 text-[11px] text-muted">
                              {cmd.duration_ms} ms
                            </p>
                          )}
                          {cmd.error && (
                            <p className="mt-2 whitespace-pre-wrap text-xs text-rose-200">
                              {cmd.error}
                            </p>
                          )}
                          {(cmd.command ||
                            cmd.stdout_excerpt ||
                            cmd.stderr_excerpt) && (
                            <details className="mt-2 text-xs text-muted">
                              <summary className="cursor-pointer">
                                Command output
                              </summary>
                              {cmd.command && (
                                <p className="mt-2 break-all font-mono text-[11px]">
                                  $ {cmd.command}
                                </p>
                              )}
                              {cmd.stdout_excerpt && (
                                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-black/20 p-2">
                                  stdout: {cmd.stdout_excerpt}
                                </pre>
                              )}
                              {cmd.stderr_excerpt && (
                                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-black/20 p-2">
                                  stderr: {cmd.stderr_excerpt}
                                </pre>
                              )}
                            </details>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {security && (
                    <div
                      className={`rounded-xl border p-4 ${staleSecurity ? 'border-amber-800 bg-amber-950/20' : 'border-line bg-elevated/50'}`}
                    >
                      <div className="flex justify-between">
                        <span className="font-semibold">Security review</span>
                        <Badge tone={statusTone(security.overall_status)}>
                          {staleSecurity
                            ? 'Stale'
                            : label(security.overall_status)}
                        </Badge>
                      </div>
                      <p className="mt-2 text-xs leading-5 text-muted">
                        {security.summary}
                      </p>
                      {security.findings.map((finding, index) => (
                        <div
                          className="mt-3 rounded-lg border border-line p-3"
                          key={index}
                        >
                          <p className="text-xs font-semibold text-rose-200">
                            {finding.severity}: {finding.explanation}
                          </p>
                          <p className="mt-1 text-xs text-muted">
                            {finding.file_path || 'Repository'}
                            {finding.line ? `:${finding.line}` : ''} ·{' '}
                            {label(finding.source)}
                          </p>
                          <p className="mt-2 whitespace-pre-wrap text-xs text-zinc-300">
                            Evidence: {finding.evidence}
                          </p>
                          <p className="mt-2 whitespace-pre-wrap text-xs text-zinc-300">
                            Suggested remediation: {finding.remediation}
                          </p>
                        </div>
                      ))}
                      {security.review_limitations.map((limitation, index) => (
                        <p
                          className="mt-2 text-xs text-muted"
                          key={`limitation-${index}`}
                        >
                          Review limitation: {limitation}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
                {pr && (
                  <div className="rounded-xl border border-violet/30 bg-violet/5 p-4">
                    <p className="text-sm font-semibold">
                      Pull request · {pr.status}
                    </p>
                    {pr.pr_url && (
                      <a
                        className="mt-2 inline-block text-sm text-mint underline"
                        href={pr.pr_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open confirmed GitHub pull request
                      </a>
                    )}
                    <div className="mt-2 grid gap-1 text-xs text-muted">
                      <p>
                        Base: {pr.base_branch} · Head: {pr.branch_name}
                      </p>
                      {pr.commit_sha && (
                        <p>
                          Commit: <code>{pr.commit_sha}</code>
                        </p>
                      )}
                      {pr.merge_commit_sha && (
                        <p>
                          Merge commit: <code>{pr.merge_commit_sha}</code>
                        </p>
                      )}
                      {pr.merged_at && (
                        <p>Merged: {new Date(pr.merged_at).toLocaleString()}</p>
                      )}
                      {pr.error_details && (
                        <p className="whitespace-pre-wrap text-rose-200">
                          Publication error: {pr.error_details}
                        </p>
                      )}
                      {pr.status === 'CREATED' && role !== 'viewer' && (
                        <button
                          className="mt-3 inline-flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-xs font-semibold hover:bg-elevated"
                          disabled={busy}
                          onClick={() => void action('sync-pr')}
                          type="button"
                        >
                          <RefreshCw size={14} /> Sync GitHub status
                        </button>
                      )}
                    </div>
                  </div>
                )}
                <div className="rounded-xl border border-line bg-elevated/40 p-4">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold">
                      Approval decisions
                    </span>
                    <Badge>{approvals.length}</Badge>
                  </div>
                  {approvals.length ? (
                    <div className="mt-3 grid gap-3">
                      {approvals.map((decision) => (
                        <div key={decision.id}>
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone={statusTone(decision.status)}>
                              {label(decision.status)}
                            </Badge>
                            <span className="text-xs text-muted">
                              {decision.approver_id} ·{' '}
                              {fullDate(decision.decided_at)}
                            </span>
                          </div>
                          <p className="mt-2 text-xs leading-5 text-zinc-300">
                            {decision.reason}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="mt-2 text-xs text-muted">
                      {task.approval_required
                        ? 'No decision has been recorded.'
                        : 'No approval was required.'}
                    </p>
                  )}
                </div>
                {!eligibility.eligible && task.status === 'APPROVED' && (
                  <p className="text-xs text-muted">
                    PR unavailable:{' '}
                    {eligibility.reason ||
                      'Eligibility has not been confirmed.'}
                  </p>
                )}
                {previousExecutions.length > 0 && (
                  <details className="rounded-xl border border-line p-4">
                    <summary className="cursor-pointer text-sm font-semibold">
                      Previous execution attempts ({previousExecutions.length})
                    </summary>
                    <div className="mt-3 grid gap-3">
                      {previousExecutions.map((execution) => (
                        <div
                          className="rounded-lg bg-elevated/50 p-3"
                          key={execution.id}
                        >
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone={statusTone(execution.status)}>
                              {label(execution.status)}
                            </Badge>
                            <span className="text-xs text-muted">
                              {fullDate(execution.created_at)} · {execution.id}
                            </span>
                          </div>
                          {execution.branch_reference && (
                            <p className="mt-2 break-all text-xs text-muted">
                              Branch: <code>{execution.branch_reference}</code>
                            </p>
                          )}
                          {execution.patch_summary && (
                            <p className="mt-2 text-xs text-zinc-300">
                              {execution.patch_summary}
                            </p>
                          )}
                          {execution.error_details && (
                            <p className="mt-2 whitespace-pre-wrap text-xs text-rose-200">
                              {execution.error_details}
                            </p>
                          )}
                          {execution.patch_fingerprint && (
                            <button
                              className="mt-2 text-xs font-semibold text-mint hover:underline"
                              disabled={patchPreviewLoading}
                              onClick={() => void showPatch(execution.id)}
                              type="button"
                            >
                              View this attempt’s patch
                            </button>
                          )}
                          {patchPreviewError &&
                            patchPreviewTarget === execution.id && (
                              <p className="mt-2 text-xs text-rose-200">
                                {patchPreviewError}
                              </p>
                            )}
                          {patchPreview?.execution_id === execution.id && (
                            <details className="mt-2 rounded border border-line p-2">
                              <summary className="cursor-pointer text-xs">
                                Patch preview ·{' '}
                                {patchPreview.committed
                                  ? 'committed'
                                  : 'uncommitted'}
                              </summary>
                              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[10px] leading-4">
                                {patchPreview.diff ||
                                  'No textual diff is available.'}
                              </pre>
                            </details>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            ) : (
              <EmptyState
                title="No execution yet"
                description="An eligible task can be started after analysis."
              />
            )}
          </Panel>
          <Panel className="p-5">
            <div className="flex flex-wrap items-center gap-2">
              <ActivityIcon size={17} className="text-mint" />
              <h2 className="font-display text-lg font-semibold">
                Execution log
              </h2>
              {latest && (
                <Badge tone={statusTone(latest.status)}>
                  {label(latest.status)}
                </Badge>
              )}
              {executionIsLive && (
                <span className="ml-auto text-xs text-mint">
                  Live · refreshes every 5 seconds
                </span>
              )}
            </div>
            {executionEvents.length ? (
              <ol
                aria-live="polite"
                className="mt-4 max-h-96 divide-y divide-line overflow-y-auto"
              >
                {executionEvents.map((event) => (
                  <li key={event.id} className="flex gap-3 py-3 first:pt-0">
                    <span
                      className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${/failed|blocked|rejected/i.test(event.event_type) ? 'bg-rose-400' : 'bg-mint'}`}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold">
                          {label(event.event_type)}
                        </span>
                        <span className="ml-auto text-xs text-muted">
                          {fullDate(event.occurred_at)}
                        </span>
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-muted">
                        {event.message}
                      </p>
                      {Object.keys(event.metadata).length > 0 && (
                        <details className="mt-2 text-xs text-muted">
                          <summary className="cursor-pointer">Details</summary>
                          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-elevated p-3 font-mono text-[11px] leading-5 text-zinc-300">
                            {JSON.stringify(event.metadata, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="mt-3 text-sm text-muted">
                {latest
                  ? 'Execution events will appear here as the worker progresses.'
                  : 'Start an execution to see its live progress here.'}
              </p>
            )}
          </Panel>
          <Panel className="p-5">
            <div className="flex items-center gap-2">
              <ActivityIcon size={17} className="text-mint" />
              <h2 className="font-display text-lg font-semibold">Activity</h2>
            </div>
            <div className="mt-4">
              <ActivityList events={dedupeActivity(history)} />
              {historyHasMore && (
                <Button
                  className="mt-4"
                  loading={historyLoadingMore}
                  variant="secondary"
                  onClick={() => void loadOlderHistory()}
                >
                  Load older activity
                </Button>
              )}
            </div>
          </Panel>
          <Panel className="p-5">
            <div className="flex items-center gap-2">
              <ActivityIcon size={17} className="text-mint" />
              <h2 className="font-display text-lg font-semibold">
                Status history
              </h2>
              <Badge>{task.transition_history.length}</Badge>
            </div>
            {task.transition_history.length ? (
              <ol className="mt-4 grid gap-3">
                {[...task.transition_history]
                  .reverse()
                  .map((transition, index) => (
                    <li
                      className="rounded-lg border border-line bg-elevated/30 p-3"
                      key={`${transition.at}-${transition.to_status}-${index}`}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge>
                          {label(transition.from_status || 'CREATED')}
                        </Badge>
                        <span aria-hidden="true" className="text-muted">
                          →
                        </span>
                        <Badge tone={statusTone(transition.to_status)}>
                          {label(transition.to_status)}
                        </Badge>
                        <span className="ml-auto text-xs text-muted">
                          {fullDate(transition.at)}
                        </span>
                      </div>
                      <p className="mt-2 text-sm text-zinc-300">
                        {transition.reason}
                      </p>
                      <p className="mt-1 text-xs text-muted">
                        {label(transition.actor)}
                        {transition.evidence
                          ? ` · ${label(transition.evidence.kind)} evidence: ${transition.evidence.reference}`
                          : ''}
                      </p>
                    </li>
                  ))}
              </ol>
            ) : (
              <p className="mt-3 text-sm text-muted">
                No status transitions have been recorded.
              </p>
            )}
          </Panel>
        </div>
        <aside className="grid content-start gap-4">
          <Panel className="p-5">
            <p className="eyebrow">WORKFLOW SNAPSHOT</p>
            <div className="mt-4 grid gap-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted">Approval</span>
                <span>{approvalLabel(task)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted">Updated</span>
                <span>{relativeDate(task.updated_at)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted">Executions</span>
                <span>{executions.length}</span>
              </div>
            </div>
          </Panel>
          <Panel className="p-5">
            <p className="eyebrow">WORKERS</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {task.assigned_agents.length ? (
                task.assigned_agents.map((agent) => (
                  <Badge key={agent}>{label(agent)}</Badge>
                ))
              ) : (
                <span className="text-sm text-muted">Not assigned yet</span>
              )}
            </div>
          </Panel>
        </aside>
      </div>
    </>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<CommandCenter />} />
        <Route path="board" element={<WorkforceBoard />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="tasks/:taskId" element={<TaskDetail />} />
        <Route path="activity" element={<ActivityPage />} />
        <Route path="approvals" element={<ApprovalsPage />} />
        <Route path="repositories" element={<RepositoriesPage />} />
        <Route path="skills" element={<SkillsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
