# Skills for the Antigravity coding agent

## Agreed behavior

Skills are managed by NEXUS and supplied to the coding agent at runtime. Users
can create global skills and skills scoped to a connected repository. Each
execution automatically loads enabled global skills and enabled skills for its
approved repository. Skills are not written into connected repositories.

The Antigravity runtime is the default coding runtime. Operators can explicitly
select the direct Gemini adapter for compatibility or comparison.

## Implementation plan

1. **Define skill records and persistence**
   - Store each skill's ID, name, description, Markdown instructions, scope,
     optional repository key, enabled state, version, creator, and timestamps.
   - Provide Firestore storage for shared deployments and in-memory storage for
     tests.
   - Validate global versus repository scope and ensure repository-scoped
     skills refer to currently approved repositories.

2. **Add authenticated skill management APIs**
   - Support listing, creating, updating, enabling/disabling, and deleting
     skills.
   - A repository-filtered list returns global skills plus that repository's
     skills. A list without a repository filter returns global skills.
   - Require admin access for global-skill changes and operator/admin access
     for repository-specific changes.
   - Enforce per-skill and combined runtime size limits and report invalid
     input explicitly.

3. **Add a Skills management page**
   - Let users filter by connected repository and manage global or
     repository-specific skills.
   - Support creating/editing Markdown instructions, importing a `SKILL.md`
     file as app-managed content, enabling/disabling, and deleting skills.
   - Surface server permission and validation errors instead of silently
     hiding failed changes.

4. **Use skills in coding executions**
   - Default coding execution to Antigravity, with an explicit setting to use
     direct Gemini instead.
   - Resolve enabled global and matching-repository skills once when an
     execution begins; reuse that snapshot for all proposals and repair rounds.
   - Add the instructions as delimited guidance without changing the existing
     safety rules or granting command execution.
   - Record skill IDs, versions, and content hashes in execution activity
     without logging complete instruction bodies.

5. **Validate and document**
   - Cover scope matching, disabled skills, role checks, repository approval,
     size limits, imports, snapshots, and prompt inclusion in tests.
   - Verify the local and Cloud Run workers resolve skills through the same
     Firestore collection and prompt-building path.
   - Document runtime selection, skill permissions, scoping, and limits.

## Delivery order

Backend skill records, storage, and APIs come first. Runtime injection and
execution traceability follow, then the Skills page, and finally focused tests
and operational documentation.

## Implementation status

- [x] Persist versioned global and repository-scoped skills in Firestore and
  in-memory test storage.
- [x] Add role-protected skill management APIs and the Skills page, including
  Markdown import.
- [x] Make Antigravity the default runtime and inject one bounded matching
  skill snapshot into coding and repair proposals.
- [x] Add runtime activity fingerprints, tests, and operational documentation.
