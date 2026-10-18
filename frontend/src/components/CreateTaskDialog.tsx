import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/authState';
import { useToast } from '../contexts/toastState';
import { useDialogFocus } from '../hooks/useDialogFocus';
import {
  createTask,
  listRepositories,
  uploadTaskAttachments,
} from '../lib/api';
import type { TaskType } from '../domain/types';
import { Button, fieldClass } from './ui';
export function CreateTaskDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const close = useCallback(onClose, [onClose]);
  useDialogFocus(open, root, close);
  const { user } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [repos, setRepos] = useState<Array<{ key: string; label: string }>>([]);
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [repoError, setRepoError] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [repository, setRepository] = useState('');
  const [taskType, setTaskType] = useState('');
  const [attachments, setAttachments] = useState<File[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open || !user) return;
    const controller = new AbortController();
    setLoadingRepos(true);
    setRepoError('');
    listRepositories(user, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setRepos(result.items);
      })
      .catch((e) => {
        if (!controller.signal.aborted)
          setRepoError(
            e instanceof Error ? e.message : 'Could not load repositories.',
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingRepos(false);
      });
    return () => controller.abort();
  }, [open, user]);
  if (!open) return null;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!title.trim() || !description.trim() || !repository) return;
    setBusy(true);
    setError('');
    try {
      const task = await createTask(user, {
        title: title.trim(),
        description: description.trim(),
        repository_key: repository,
        ...(taskType ? { task_type: taskType as TaskType } : {}),
      });
      if (attachments.length) {
        try {
          await uploadTaskAttachments(user, task.id, attachments);
        } catch (uploadError) {
          toast(
            'error',
            `Task created, but attachments could not be uploaded: ${uploadError instanceof Error ? uploadError.message : 'upload failed'}`,
          );
          onClose();
          navigate(`/tasks/${task.id}`);
          return;
        }
      }
      toast('success', 'Task created and added to the workforce.');
      setTitle('');
      setDescription('');
      setRepository('');
      setTaskType('');
      setAttachments([]);
      onClose();
      navigate(`/tasks/${task.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Task could not be created.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="fixed inset-0 z-[70] overflow-y-auto bg-black/40 p-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="grid min-h-full place-items-center">
        <div
          ref={root}
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-title"
          className="my-5 w-full max-w-xl rounded-2xl border border-line bg-panel p-6 shadow-xl"
        >
          <div className="flex justify-between">
            <div>
              <p className="eyebrow">WORK INTAKE</p>
              <h2
                id="create-title"
                className="mt-2 font-display text-2xl font-bold"
              >
                Create a task
              </h2>
            </div>
            <button aria-label="Close" onClick={onClose}>
              ✕
            </button>
          </div>
          <form className="mt-6 grid gap-4" onSubmit={submit}>
            <label className="field-label">
              Title
              <input
                className={fieldClass}
                maxLength={200}
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
            <label className="field-label">
              Attachments{' '}
              <span className="font-normal text-muted">
                (optional, up to 10 files / 20 MB)
              </span>
              <input
                className={`${fieldClass} file:mr-3 file:rounded-lg file:border-0 file:bg-primary/10 file:px-3 file:py-2 file:text-xs file:font-semibold file:text-primary`}
                type="file"
                multiple
                accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.txt,.md,.csv,.json,application/pdf,image/*,text/plain,text/markdown,text/csv,application/json"
                onChange={(event) => {
                  const selected = Array.from(event.target.files ?? []);
                  setAttachments((current) =>
                    [...current, ...selected].slice(0, 10),
                  );
                  event.target.value = '';
                }}
              />
              {attachments.length > 0 && (
                <span className="mt-2 grid gap-1.5">
                  {attachments.map((file, index) => (
                    <span
                      key={`${file.name}-${index}`}
                      className="flex items-center justify-between gap-3 rounded-lg bg-elevated px-3 py-2 text-xs"
                    >
                      <span className="truncate">
                        {file.name} · {(file.size / 1024).toFixed(0)} KB
                      </span>
                      <button
                        type="button"
                        className="shrink-0 text-red-600"
                        onClick={() =>
                          setAttachments((items) =>
                            items.filter((_, i) => i !== index),
                          )
                        }
                      >
                        Remove
                      </button>
                    </span>
                  ))}
                </span>
              )}
            </label>
            <label className="field-label">
              Description
              <textarea
                className={fieldClass}
                rows={5}
                maxLength={20000}
                required
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
              <span className="mt-1 block text-right text-xs text-muted">
                {description.length.toLocaleString()} / 20,000
              </span>
            </label>
            <label className="field-label">
              Repository
              <select
                className={fieldClass}
                required
                value={repository}
                onChange={(e) => setRepository(e.target.value)}
              >
                <option value="">
                  {loadingRepos
                    ? 'Loading repositories…'
                    : 'Choose approved repository'}
                </option>
                {repos.map((repo) => (
                  <option value={repo.key} key={repo.key}>
                    {repo.label}
                  </option>
                ))}
              </select>
              {repoError && (
                <span className="mt-1 block text-xs text-red-600">
                  {repoError}
                </span>
              )}
              {!loadingRepos && !repoError && repos.length === 0 && (
                <span className="mt-1 block text-xs text-muted">
                  No approved repositories are configured.
                </span>
              )}
            </label>
            <label className="field-label">
              Task type{' '}
              <span className="font-normal text-muted">(optional)</span>
              <select
                className={fieldClass}
                value={taskType}
                onChange={(e) => setTaskType(e.target.value)}
              >
                <option value="">General task</option>
                {(
                  [
                    'FEATURE',
                    'BUGFIX',
                    'REFACTOR',
                    'TEST',
                    'DOCS',
                    'SECURITY',
                    'INFRASTRUCTURE',
                  ] as const
                ).map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
            </label>
            {error && (
              <p role="alert" className="text-sm text-red-600">
                {error}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={onClose}>
                Cancel
              </Button>
              <Button
                loading={busy}
                disabled={
                  !title.trim() ||
                  !description.trim() ||
                  !repository ||
                  !repos.length
                }
              >
                Create task
              </Button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
