# Coding agent skills

NEXUS skills are reusable Markdown instructions managed in the application.
They are stored in the NEXUS Firestore `skills` collection and are not written
to or committed into connected repositories. `SKILL.md` files can be imported
from the Skills page; importing copies their contents into a NEXUS-managed
skill.

## Scope and permissions

- **Global skills** apply to coding executions in every approved repository.
  Only administrators can create, edit, enable/disable, or delete them.
- **Repository skills** apply only to executions for the selected connected
  repository. Operators and administrators can manage these skills.
- Listing skills without a repository filter shows global skills. Selecting a
  repository shows global skills and skills for that repository.
- Disabled skills are not supplied to coding executions.

The authenticated API exposes `GET /api/skills` and supports `POST /api/skills`,
`PATCH /api/skills/{skill_id}`, and `DELETE /api/skills/{skill_id}` for
management. Repository scope must refer to an approved repository.

## Runtime behavior and limits

The Antigravity coding runtime is the default. Set
`NEXUS_CODING_AGENT=gemini` to use the direct Gemini adapter instead. Both
runtimes receive the same eligible skills as structured prompt guidance.

Instructions are limited to 20,000 characters per skill and the combined skill
names, descriptions, and instructions are limited to 40,000 characters per
execution. At most 20 enabled skills may apply to one execution.
The worker reports a clear execution error when the combined count or size
limit is exceeded; it does not silently truncate skill instructions.

Each execution resolves its applicable enabled skills once and reuses that
snapshot during retries and repair proposals. The execution activity records
the skill IDs, versions, and SHA-256 instruction fingerprints, but not the full
instruction text.

Skills are untrusted supplementary guidance. They cannot override the coding
agent's system safety instructions, grant new tools, authorize command
execution, or bypass repository/file-operation checks.
