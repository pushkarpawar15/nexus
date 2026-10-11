import { getIdToken } from './firebase';
import type {
  Activity,
  Approval,
  Execution,
  ExecutionDiff,
  ExecutionPlanInput,
  Page,
  PullRequest,
  SecurityReview,
  Task,
  TaskStatus,
  TaskType,
  Mode,
  ModelChoices,
  Risk,
  ValidationReport,
} from '../domain/types';
import type { User } from 'firebase/auth';

const base = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
async function request<T>(
  user: User | null,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let token: string;
  try {
    token = await getIdToken(user);
  } catch {
    throw new Error('Sign in to use the NEXUS API.');
  }
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init.body && !(init.body instanceof FormData)
          ? { 'Content-Type': 'application/json' }
          : {}),
        Authorization: `Bearer ${token}`,
        ...init.headers,
      },
    });
  } catch {
    throw new Error(
      'Cannot reach the NEXUS API. Check that the backend is running.',
    );
  }
  if (response.status === 204) return undefined as T;
  const data = (await response.json().catch(() => ({}))) as {
    error?: { message?: string };
    detail?: unknown;
    message?: string;
  };
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail
          .map((item) => (item as { msg?: string }).msg)
          .filter(Boolean)
          .join(', ')
      : data.detail;
    throw new Error(
      data.error?.message ||
        (typeof detail === 'string' ? detail : undefined) ||
        data.message ||
        `Request failed (${response.status})`,
    );
  }
  return data as T;
}
const q = (values: Record<string, string | number | undefined>) => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(
    ([key, value]) => value !== undefined && params.set(key, String(value)),
  );
  return `?${params}`;
};
const id = (value: string) => encodeURIComponent(value);
export interface ConnectedRepository {
  key: string;
  label: string;
  owner?: string;
  repo?: string;
  base_branch?: string;
  private?: boolean;
}
export const listRepositories = (u: User | null, signal?: AbortSignal) =>
  request<{ items: ConnectedRepository[] }>(
    u,
    '/api/repositories',
    signal ? { signal } : {},
  );
export const getGitHubConnection = (u: User | null) =>
  request<{
    configured: boolean;
    connected: boolean;
    account_login: string | null;
    mode?: 'app' | 'pat' | null;
    repository?: string | null;
    repository_info?: ConnectedRepository | null;
    pat_supported?: boolean;
  }>(u, '/api/github/connection');
export const beginGitHubInstall = (u: User | null) =>
  request<{ url: string }>(u, '/api/github/install-url', { method: 'POST' });
export const connectGitHubPAT = (
  u: User | null,
  body: { token: string; owner: string; repo: string },
) =>
  request<{
    configured: boolean;
    connected: boolean;
    account_login: string;
    mode: 'pat';
    repository: string;
  }>(u, '/api/github/pat', { method: 'POST', body: JSON.stringify(body) });
export const disconnectGitHubPAT = (u: User | null) =>
  request<void>(u, '/api/github/pat', { method: 'DELETE' });
export const disconnectGitHub = (u: User | null) =>
  request<void>(u, '/api/github/connection', { method: 'DELETE' });
export const uploadTaskAttachments = (
  u: User | null,
  taskId: string,
  files: File[],
) => {
  const body = new FormData();
  files.forEach((file) => body.append('files', file));
  return request<{ items: import('../domain/types').TaskAttachment[] }>(
    u,
    `/api/tasks/${id(taskId)}/attachments`,
    { method: 'POST', body },
  );
};
export const deleteTaskAttachment = (
  u: User | null,
  taskId: string,
  attachmentId: string,
) =>
  request<void>(u, `/api/tasks/${id(taskId)}/attachments/${id(attachmentId)}`, {
    method: 'DELETE',
  });
export async function downloadTaskAttachment(
  u: User | null,
  taskId: string,
  attachmentId: string,
  filename: string,
) {
  const token = await getIdToken(u);
  const response = await fetch(
    `${base}/api/tasks/${id(taskId)}/attachments/${id(attachmentId)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok)
    throw new Error(`Attachment download failed (${response.status}).`);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
export const listTasks = (
  u: User | null,
  limit = 100,
  offset = 0,
  status?: TaskStatus,
) => request<Page<Task>>(u, `/api/tasks${q({ limit, offset, status })}`);
export async function listAllTasks(u: User | null): Promise<Task[]> {
  const result: Task[] = [];
  let offset = 0;
  for (;;) {
    const page = await listTasks(u, 100, offset);
    result.push(...page.items);
    offset += page.items.length;
    if (!page.items.length || offset >= page.total) return result;
  }
}
export const getTask = (u: User | null, taskId: string) =>
  request<Task>(u, `/api/tasks/${id(taskId)}`);
export const createTask = (
  u: User | null,
  body: {
    title: string;
    description: string;
    repository_key: string;
    task_type?: TaskType;
  },
) =>
  request<Task>(u, '/api/tasks', {
    method: 'POST',
    body: JSON.stringify(body),
  });
export const getModelChoices = (u: User | null) =>
  request<ModelChoices>(u, '/api/models');
export const analyzeTask = (
  u: User | null,
  taskId: string,
  model_id?: string,
) =>
  request<Task>(u, `/api/tasks/${id(taskId)}/analyze`, {
    method: 'POST',
    body: JSON.stringify({ model_id }),
  });
export const updateTaskPlan = (
  u: User | null,
  taskId: string,
  plan: ExecutionPlanInput,
) =>
  request<Task>(u, `/api/tasks/${id(taskId)}/plan`, {
    method: 'PATCH',
    body: JSON.stringify(plan),
  });
export const cloneTaskPlan = (
  u: User | null,
  taskId: string,
  plan: ExecutionPlanInput,
) =>
  request<Task>(u, `/api/tasks/${id(taskId)}/plan/clone`, {
    method: 'POST',
    body: JSON.stringify(plan),
  });
export const setTaskMode = (
  u: User | null,
  taskId: string,
  mode: Mode,
  risk?: Risk,
  risk_reason?: string,
) =>
  request<Task>(u, `/api/tasks/${id(taskId)}/mode`, {
    method: 'PATCH',
    body: JSON.stringify({ mode, ...(risk ? { risk, risk_reason } : {}) }),
  });
export const listTaskExecutions = (u: User | null, taskId: string) =>
  request<Execution[]>(u, `/api/tasks/${id(taskId)}/executions`);
export const getExecution = (u: User | null, executionId: string) =>
  request<Execution>(u, `/api/executions/${id(executionId)}`);
export const getExecutionDiff = (u: User | null, executionId: string) =>
  request<ExecutionDiff>(u, `/api/executions/${id(executionId)}/diff`);
export const listTaskHistory = (u: User | null, taskId: string) =>
  request<Activity[]>(u, `/api/tasks/${id(taskId)}/history`);
export const listExecutionActivity = (u: User | null, executionId: string) =>
  request<Activity[]>(u, `/api/executions/${id(executionId)}/activity`);
export const listRecentActivity = (u: User | null, limit = 50) =>
  request<Activity[]>(u, `/api/activity${q({ limit })}`);
export const startExecution = (
  u: User | null,
  taskId: string,
  model_id?: string,
) =>
  request<Execution>(u, `/api/tasks/${id(taskId)}/executions`, {
    method: 'POST',
    body: JSON.stringify({ model_id }),
  });
export const retryExecution = (
  u: User | null,
  taskId: string,
  model_id?: string,
) =>
  request<Execution>(u, `/api/tasks/${id(taskId)}/executions/retry`, {
    method: 'POST',
    body: JSON.stringify({ model_id }),
  });
export const answerWorkflowQuestion = (
  u: User | null,
  taskId: string,
  answer: string,
) =>
  request<Execution | Task>(u, `/api/tasks/${id(taskId)}/input`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  });
export const stopExecution = (
  u: User | null,
  taskId: string,
  executionId: string,
) =>
  request<Execution>(
    u,
    `/api/tasks/${id(taskId)}/executions/${id(executionId)}/stop`,
    { method: 'POST' },
  );
export const getValidationReport = (u: User | null, executionId: string) =>
  request<ValidationReport | null>(
    u,
    `/api/executions/${id(executionId)}/validation`,
  );
export const getSecurityReview = (u: User | null, executionId: string) =>
  request<SecurityReview | null>(
    u,
    `/api/executions/${id(executionId)}/security`,
  );
export const listApprovals = (u: User | null, taskId: string) =>
  request<Approval[]>(u, `/api/tasks/${id(taskId)}/approvals`);
export const decideApproval = (
  u: User | null,
  taskId: string,
  executionId: string,
  body: { status: 'APPROVED' | 'REJECTED'; reason: string },
) =>
  request<Approval>(
    u,
    `/api/tasks/${id(taskId)}/executions/${id(executionId)}/approval`,
    { method: 'POST', body: JSON.stringify(body) },
  );
export const getPullRequest = (u: User | null, executionId: string) =>
  request<PullRequest | null>(
    u,
    `/api/executions/${id(executionId)}/pull-request`,
  );
export const getPullRequestEligibility = (
  u: User | null,
  executionId: string,
) =>
  request<{ eligible: boolean; reason: string | null }>(
    u,
    `/api/executions/${id(executionId)}/pull-request-eligibility`,
  );
export const createPullRequest = (
  u: User | null,
  taskId: string,
  executionId: string,
) =>
  request<PullRequest>(
    u,
    `/api/tasks/${id(taskId)}/executions/${id(executionId)}/pull-request`,
    { method: 'POST' },
  );
export const syncPullRequest = (
  u: User | null,
  taskId: string,
  executionId: string,
) =>
  request<PullRequest>(
    u,
    `/api/tasks/${id(taskId)}/executions/${id(executionId)}/pull-request/sync`,
    { method: 'POST' },
  );
