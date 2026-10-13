import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { FileUp, Pencil, Plus, Save, Trash2, X } from 'lucide-react';
import { useAuth } from '../contexts/authState';
import { useToast } from '../contexts/toastState';
import type { Skill } from '../domain/types';
import {
  createSkill,
  deleteSkill,
  listRepositories,
  listSkills,
  updateSkill,
  type ConnectedRepository,
  type SkillWriteInput,
} from '../lib/api';
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  fieldClass,
} from './ui';

const emptyDraft: SkillWriteInput = {
  name: '',
  description: '',
  instructions: '',
  scope: 'global',
  repository_key: null,
  enabled: true,
};

export function SkillsPage() {
  const { user } = useAuth();
  const showToast = useToast();
  const [repositories, setRepositories] = useState<ConnectedRepository[]>([]);
  const [repositoryKey, setRepositoryKey] = useState('');
  const [skills, setSkills] = useState<Skill[]>([]);
  const [draft, setDraft] = useState<SkillWriteInput>(emptyDraft);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [result, connected] = await Promise.all([
        listSkills(user, repositoryKey || undefined),
        listRepositories(user),
      ]);
      setSkills(result.items);
      setRepositories(connected.items);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : 'Could not load skills.',
      );
    } finally {
      setLoading(false);
    }
  }, [repositoryKey, user]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function beginEdit(skill: Skill) {
    setEditingId(skill.id);
    setDraft({
      name: skill.name,
      description: skill.description,
      instructions: skill.instructions,
      scope: skill.scope,
      repository_key: skill.repository_key,
      enabled: skill.enabled,
    });
  }

  function resetDraft() {
    setEditingId(null);
    setDraft(emptyDraft);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    try {
      const body = {
        ...draft,
        name: draft.name.trim(),
        description: draft.description.trim(),
        repository_key:
          draft.scope === 'repository' ? draft.repository_key : null,
      };
      if (editingId) await updateSkill(user, editingId, body);
      else await createSkill(user, body);
      showToast('success', editingId ? 'Skill updated.' : 'Skill created.');
      resetDraft();
      await refresh();
    } catch (cause) {
      showToast(
        'error',
        cause instanceof Error ? cause.message : 'Could not save this skill.',
      );
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(skill: Skill) {
    try {
      await updateSkill(user, skill.id, {
        name: skill.name,
        description: skill.description,
        instructions: skill.instructions,
        scope: skill.scope,
        repository_key: skill.repository_key,
        enabled: !skill.enabled,
      });
      await refresh();
    } catch (cause) {
      showToast(
        'error',
        cause instanceof Error ? cause.message : 'Could not update this skill.',
      );
    }
  }

  async function remove(skill: Skill) {
    if (!window.confirm(`Delete the "${skill.name}" skill?`)) return;
    try {
      await deleteSkill(user, skill.id);
      showToast('success', 'Skill deleted.');
      if (editingId === skill.id) resetDraft();
      await refresh();
    } catch (cause) {
      showToast(
        'error',
        cause instanceof Error ? cause.message : 'Could not delete this skill.',
      );
    }
  }

  async function importSkill(file: File | undefined) {
    if (!file) return;
    if (file.size > 80_000) {
      showToast('error', 'Markdown imports must be 80 KB or smaller.');
      return;
    }
    try {
      const instructions = await file.text();
      setDraft((current) => ({
        ...current,
        instructions,
        name:
          current.name || file.name.replace(/\.md$/i, '').replace(/[-_]/g, ' '),
      }));
      showToast(
        'info',
        'Markdown imported into the editor. Save to store it in NEXUS.',
      );
    } catch {
      showToast('error', 'Could not read the selected Markdown file.');
    }
  }

  return (
    <div>
      <div className="mb-7">
        <p className="eyebrow">CODING AGENT</p>
        <h1 className="mt-2 font-display text-3xl font-bold tracking-tight sm:text-4xl">
          Skills
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          Reusable instructions for Antigravity. Enabled global skills and
          skills for a task&apos;s repository are automatically included in each
          coding run.
        </p>
      </div>

      <Panel className="mb-6 p-5">
        <label className="grid max-w-xl gap-2 text-sm font-semibold">
          Repository scope
          <select
            className={fieldClass}
            value={repositoryKey}
            onChange={(event) => setRepositoryKey(event.target.value)}
          >
            <option value="">Global skills only</option>
            {repositories.map((repository) => (
              <option key={repository.key} value={repository.key}>
                {repository.label}
              </option>
            ))}
          </select>
        </label>
        <p className="mt-2 text-xs text-muted">
          Repository views include global skills and skills scoped to that
          repository.
        </p>
      </Panel>

      {error && <ErrorState message={error} retry={() => void refresh()} />}

      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(20rem,0.8fr)]">
        <section>
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="font-display text-xl font-semibold">
              Managed skills
            </h2>
            <Button variant="secondary" onClick={resetDraft}>
              <Plus size={15} /> New skill
            </Button>
          </div>
          {loading ? (
            <LoadingState label="Loading skills…" />
          ) : skills.length ? (
            <div className="grid gap-3">
              {skills.map((skill) => (
                <Panel className="p-4" key={skill.id}>
                  <div className="flex flex-wrap items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="font-semibold">{skill.name}</h3>
                        <Badge tone={skill.enabled ? 'success' : 'neutral'}>
                          {skill.enabled ? 'Enabled' : 'Disabled'}
                        </Badge>
                        <Badge>
                          {skill.scope === 'global'
                            ? 'Global'
                            : repositories.find(
                                (item) => item.key === skill.repository_key,
                              )?.label || skill.repository_key}
                        </Badge>
                      </div>
                      <p className="mt-2 text-sm leading-5 text-muted">
                        {skill.description}
                      </p>
                      <p className="mt-2 text-xs text-muted">
                        Version {skill.version} · Updated{' '}
                        {new Date(skill.updated_at).toLocaleString()}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      <Button
                        variant="ghost"
                        aria-label={`${skill.enabled ? 'Disable' : 'Enable'} ${skill.name}`}
                        onClick={() => void toggleEnabled(skill)}
                      >
                        {skill.enabled ? 'Disable' : 'Enable'}
                      </Button>
                      <Button
                        variant="ghost"
                        aria-label={`Edit ${skill.name}`}
                        onClick={() => beginEdit(skill)}
                      >
                        <Pencil size={15} />
                      </Button>
                      <Button
                        variant="ghost"
                        aria-label={`Delete ${skill.name}`}
                        onClick={() => void remove(skill)}
                      >
                        <Trash2 size={15} />
                      </Button>
                    </div>
                  </div>
                  <details className="mt-3 text-xs text-muted">
                    <summary className="cursor-pointer">
                      Preview instructions
                    </summary>
                    <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-elevated p-3 font-mono text-[11px] leading-5 text-zinc-300">
                      {skill.instructions}
                    </pre>
                  </details>
                </Panel>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No skills in this scope"
              description="Create a reusable skill or import a SKILL.md file to get started."
            />
          )}
        </section>

        <Panel className="p-5">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="eyebrow">
                {editingId ? 'EDIT SKILL' : 'NEW SKILL'}
              </p>
              <h2 className="mt-1 font-display text-xl font-semibold">
                {editingId ? 'Update instructions' : 'Create a skill'}
              </h2>
            </div>
            {editingId && (
              <Button
                variant="ghost"
                aria-label="Cancel editing"
                onClick={resetDraft}
              >
                <X size={16} />
              </Button>
            )}
          </div>
          <form className="grid gap-4" onSubmit={(event) => void save(event)}>
            <label className="text-sm font-semibold">
              Name
              <input
                className={fieldClass}
                maxLength={100}
                required
                value={draft.name}
                onChange={(event) =>
                  setDraft({ ...draft, name: event.target.value })
                }
              />
            </label>
            <label className="text-sm font-semibold">
              Short description
              <textarea
                className={fieldClass}
                maxLength={500}
                required
                rows={2}
                value={draft.description}
                onChange={(event) =>
                  setDraft({ ...draft, description: event.target.value })
                }
              />
            </label>
            <label className="text-sm font-semibold">
              Scope
              <select
                className={fieldClass}
                value={draft.scope}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    scope: event.target.value as SkillWriteInput['scope'],
                    repository_key:
                      event.target.value === 'repository'
                        ? draft.repository_key || repositories[0]?.key || null
                        : null,
                  })
                }
              >
                <option value="global">Global</option>
                <option value="repository">One connected repository</option>
              </select>
            </label>
            {draft.scope === 'repository' && (
              <label className="text-sm font-semibold">
                Connected repository
                <select
                  className={fieldClass}
                  required
                  value={draft.repository_key || ''}
                  onChange={(event) =>
                    setDraft({ ...draft, repository_key: event.target.value })
                  }
                >
                  <option disabled value="">
                    Choose a connected repository
                  </option>
                  {repositories.map((repository) => (
                    <option key={repository.key} value={repository.key}>
                      {repository.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="text-sm font-semibold">
              Markdown instructions
              <textarea
                className={`${fieldClass} font-mono text-xs leading-5`}
                maxLength={20_000}
                minLength={1}
                required
                rows={12}
                value={draft.instructions}
                onChange={(event) =>
                  setDraft({ ...draft, instructions: event.target.value })
                }
              />
              <span className="mt-1 block text-right text-xs font-normal text-muted">
                {draft.instructions.length.toLocaleString()} / 20,000 characters
              </span>
            </label>
            <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
              <FileUp size={15} />
              Import `SKILL.md`
              <input
                className="sr-only"
                accept=".md,text/markdown,text/plain"
                type="file"
                onChange={(event) => {
                  void importSkill(event.target.files?.[0]);
                  event.target.value = '';
                }}
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-zinc-300">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(event) =>
                  setDraft({ ...draft, enabled: event.target.checked })
                }
              />
              Enable for matching coding executions
            </label>
            <Button loading={saving} type="submit">
              <Save size={15} /> {editingId ? 'Save changes' : 'Create skill'}
            </Button>
            <p className="text-xs leading-5 text-muted">
              Global skill changes require an administrator. Instructions are
              bounded guidance and cannot grant the agent additional tools.
            </p>
          </form>
        </Panel>
      </div>
    </div>
  );
}
