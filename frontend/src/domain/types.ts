export type TaskStatus =
  | 'BACKLOG'
  | 'ANALYZING'
  | 'TRIAGED'
  | 'READY'
  | 'HUMAN_QUEUE'
  | 'IN_PROGRESS'
  | 'VALIDATING'
  | 'SECURITY_REVIEW'
  | 'AWAITING_APPROVAL'
  | 'AWAITING_INPUT'
  | 'APPROVED'
  | 'REJECTED'
  | 'PR_CREATED'
  | 'COMPLETED'
  | 'FAILED'
  | 'BLOCKED';
export type TaskType =
  | 'BUGFIX'
  | 'FEATURE'
  | 'REFACTOR'
  | 'DOCS'
  | 'TEST'
  | 'SECURITY'
  | 'INFRASTRUCTURE'
  | 'OTHER';
export type Risk = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type Complexity = 'LOW' | 'MEDIUM' | 'HIGH';
export type Mode = 'AI' | 'HUMAN' | 'HYBRID';
export interface ExecutionPlanInput {
  title?: string;
  risk: Risk;
  complexity: Complexity;
  mode: Mode;
  approval_required: boolean;
  reason: string;
  assigned_agents: string[];
  steps: string[];
  expected_files_or_areas: string[];
  validation_plan: string[];
  potential_concerns: string[];
}
export interface Transition {
  from_status: TaskStatus | null;
  to_status: TaskStatus;
  actor: string;
  reason: string;
  at: string;
  evidence: { kind: string; reference: string } | null;
}
export interface ExecutionPlan {
  risk: Risk;
  complexity: Complexity;
  mode: Mode;
  assigned_agents: string[];
  approval_required: boolean;
  reason: string;
  steps: string[];
  expected_files_or_areas: string[];
  validation_plan: string[];
  potential_concerns: string[];
  source: 'GEMINI' | 'RULES_FALLBACK' | 'OPERATOR';
  model_id: string | null;
  policy_reason: string;
  security_review_required: boolean;
  operator_override?: boolean;
}
export interface Task {
  id: string;
  title: string;
  description: string;
  repository_key: string;
  repository_label?: string | null;
  task_type: TaskType;
  status: TaskStatus;
  risk: Risk | null;
  complexity: Complexity | null;
  mode: Mode | null;
  execution_plan: ExecutionPlan | null;
  approval_required: boolean | null;
  pending_question?: string | null;
  pending_execution_id?: string | null;
  human_guidance?: string | null;
  assigned_agents: string[];
  created_at: string;
  updated_at: string;
  created_by: string;
  transition_history: Transition[];
  attachments: TaskAttachment[];
  github_pr_url?: string | null;
  pr_url?: string | null;
}
export interface TaskAttachment {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  storage_key: string;
  created_at: string;
  created_by: string;
}
export interface Skill {
  id: string;
  name: string;
  description: string;
  instructions: string;
  scope: 'global' | 'repository';
  repository_key: string | null;
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  created_by: string;
  updated_by: string;
}
export type ExecutionStatus =
  'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'BLOCKED';
export interface Execution {
  id: string;
  task_id: string;
  assigned_worker: string;
  status: ExecutionStatus;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  workspace_reference: string | null;
  branch_reference: string | null;
  changed_files: string[];
  test_summary: string | null;
  security_summary: string | null;
  error_details: string | null;
  final_result: string | null;
  repository_key?: string | null;
  model_id?: string | null;
  base_commit_sha?: string | null;
  patch_fingerprint?: string | null;
  patch_summary?: string | null;
}
export interface ModelOption {
  id: string;
  label: string;
  description: string;
  provider?: string;
  supports_attachments?: boolean;
}
export interface ModelChoices {
  analysis_default: string;
  coding_default: string;
  analysis_provider: string;
  coding_provider: string;
  coding_location: string;
  models: ModelOption[];
}
export interface Activity {
  id: string;
  task_id: string;
  execution_id: string | null;
  worker_name: string;
  event_type: string;
  message: string;
  occurred_at: string;
  source: 'SYSTEM' | 'WORKER' | 'DEVELOPMENT_FIXTURE';
  metadata: Record<string, unknown>;
}
export interface ValidationReport {
  id: string;
  execution_id: string;
  task_id: string;
  patch_fingerprint: string;
  overall_status: 'PASSED' | 'FAILED' | 'BLOCKED' | 'SKIPPED';
  started_at: string;
  finished_at: string;
  summary: string;
  commands: Array<{
    command_id: string;
    command: string | null;
    status: string;
    exit_code: number | null;
    duration_ms: number | null;
    stdout_excerpt: string;
    stderr_excerpt: string;
    error: string | null;
  }>;
}
export interface SecurityReview {
  id: string;
  execution_id: string;
  task_id: string;
  patch_fingerprint: string;
  overall_status: 'PASSED' | 'FINDINGS' | 'BLOCKED';
  started_at: string;
  finished_at: string;
  findings: Array<{
    source: string;
    severity: string;
    file_path: string | null;
    line: number | null;
    evidence: string;
    explanation: string;
    remediation: string;
    confirmed: boolean;
  }>;
  review_limitations: string[];
  summary: string;
}
export interface Approval {
  id: string;
  task_id: string;
  execution_id: string;
  patch_fingerprint: string;
  status: 'APPROVED' | 'REJECTED';
  approver_id: string;
  decided_at: string;
  reason: string;
  policy_requirements: string[];
}
export interface PullRequest {
  id: string;
  task_id: string;
  execution_id: string;
  patch_fingerprint: string;
  repository_key: string;
  base_branch: string;
  branch_name: string;
  commit_sha: string | null;
  pr_number: number | null;
  pr_url: string | null;
  merge_commit_sha: string | null;
  merged_at: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  error_details: string | null;
}
export interface ExecutionDiff {
  execution_id: string;
  branch: string;
  base_commit_sha: string;
  commit_sha: string | null;
  committed: boolean;
  patch_fingerprint: string;
  changed_files: string[];
  diff: string;
  redacted: boolean;
}
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
