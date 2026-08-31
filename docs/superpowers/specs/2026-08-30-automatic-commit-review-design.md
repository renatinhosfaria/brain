# Automatic Commit Review Design

**Date:** 2026-08-30
**Status:** Approved by operator on 2026-08-30
**Repositories:** `/root/brain`, `/root/.hermes`

## 1. Goal

Run a complete, read-only Codex review after every new local commit in either
repository. Each review must compare the exact commit with the relevant CTWA
specifications, plans, operational contracts, and a privacy-sanitized narrative
extract of the active Claude Code session. Reviews produce durable local
reports and never change code, configuration, services, branches, or remotes.

## 2. Non-goals

- The reviewer does not fix findings automatically.
- The reviewer does not block or roll back `git commit`.
- The reviewer does not commit, push, open pull requests, restart services, or
  write to FamaChat, Brain runtime databases, Hermes state, or Kanban.
- The local reviewer does not replace a later GitHub pull-request gate.
- The reviewer does not ingest raw Claude tool results, attachments, database
  output, shell output, or unredacted secrets and customer identifiers.
- The reviewer does not modify `/usr/local/lib/hermes-agent/**` or any original
  Hermes Agent source/configuration.

## 3. Review contract

For each exact commit, the reviewer evaluates:

1. correctness and regressions introduced by the diff;
2. agreement with the CTWA architecture spec, master plan, and applicable
   component plan;
3. agreement with decisions and evidence recorded in the active Claude Code
   session narrative;
4. test adequacy, including negative, failure-order, privacy, rollout, and
   production-regression cases;
5. privacy and secret handling;
6. immutability of upstream Hermes Agent and read-only Brain access to Hermes
   state/Kanban;
7. deploy ordering, backward compatibility, rollback, and partial-deploy
   behavior;
8. operational drift between repository-owned artifacts and deployed copies;
9. whether claims in documentation and commit messages are supported by code
   and fresh evidence.

Findings use these severities:

- **P0:** immediate data loss, secret exposure, unauthorized production write,
  or broad service outage;
- **P1:** likely production regression, broken security boundary, unreachable
  acceptance gate, or silently incorrect durable state;
- **P2:** material correctness, observability, test, compatibility, or
  maintainability defect that should be fixed before the stage closes;
- **P3:** low-risk improvement or documentation mismatch.

The report verdict is exactly one of:

- `APPROVED` — no findings;
- `APPROVED_WITH_NOTES` — P3 findings only;
- `BLOCKED` — any P0, P1, or P2 finding, or an incomplete review.

An execution error, missing parent commit, failed sanitization, unavailable
Codex authentication, or truncated required context produces `BLOCKED`; it
must never be reported as a clean review.

## 4. Architecture

### 4.1 Commit hooks

Each repository receives a minimal `.git/hooks/post-commit` hook. The hook:

- resolves the repository root and new `HEAD` SHA itself;
- invokes only the fixed enqueue entrypoint installed by this project;
- performs no model call and no repository write;
- returns success after enqueueing, even if the review service is unavailable;
- records an enqueue failure in the system journal without exposing commit
  content.

The installer refuses to overwrite an existing non-managed hook. Re-running
the installer is idempotent when the managed hook is already present.

### 4.2 Durable queue and systemd activation

Queue entries live under `/var/lib/codex-commit-review/queue/`, mode `0700`, as
atomic `0600` JSON files containing only repository alias, canonical repository
path, and full commit SHA. Filenames are unique and sortable.

A systemd path unit watches the queue directory and activates a oneshot worker.
The worker uses a process lock and drains entries in creation order. This
avoids blocking commits and prevents concurrent Codex reviews. A queue entry is
removed only after a durable report or a durable failure report exists.

The worker deduplicates by `(repository, SHA)`. Re-enqueueing an already
reviewed commit is harmless. Failed reviews may be explicitly retried without
creating a commit.

### 4.3 Exact commit materialization

The worker validates repository paths against the fixed allowlist
`{/root/brain, /root/.hermes}` and validates SHAs as full hexadecimal commit
objects. It obtains the exact parent-to-commit diff from Git, independent of
the current working tree.

To keep secrets out of the model input, the worker does not give Codex direct
access to either live repository or its `.git` directory. Instead it creates a
private temporary review bundle containing:

- a sanitized unified diff for the exact commit;
- sanitized copies of changed text files at the committed revision;
- selected sanitized reference documents from both repositories;
- commit metadata excluding author email;
- the sanitized Claude narrative extract;
- the fixed review instructions.

Binary files are represented by path, size, and Git object identifiers only.
The temporary directory is removed after the report is installed.

### 4.4 Context selection

Reference routing is deterministic:

- every Brain commit receives the architecture spec, master plan, and the
  component plans whose paths or named contracts intersect the diff;
- every Hermes commit receives the architecture spec, profile-contract plan,
  relevant SOUL/SKILL/config contracts, and the master plan;
- changes involving lifecycle, writer, observer, transport, profile, or
  rollout keywords include their corresponding component plan;
- if routing is uncertain, the reviewer receives all CTWA component plans,
  bounded by the configured context-size limit.

The report lists every reference included and every reference omitted due to a
size bound. Omitting a required reference makes the review incomplete and
therefore `BLOCKED`.

### 4.5 Claude Code narrative extraction

The extractor selects the most recently modified top-level Claude Code JSONL
session under `/root/.claude/projects/-root/` whose narrative mentions the
reviewed repository or CTWA plan names. It includes only `user` and `assistant`
content blocks with `type=text` and excludes all tool calls, tool results,
attachments, sidechain records, and opaque metadata.

Only narrative entries at or before the commit timestamp are eligible. The
extract is ordered, bounded by count and bytes, and records the source session
ID, covered timestamp range, number of retained entries, and number omitted.

Sanitization replaces, at minimum:

- bearer/API tokens, authorization headers, passwords, secrets, cookies, and
  environment assignments whose names imply credentials;
- phone-like digit sequences and WhatsApp JID/LID values;
- email addresses, URL credentials and query strings;
- raw `key.id`, `ctwaClid`, `sourceId`, client IDs, message IDs, turn/event
  identifiers, and long high-entropy strings;
- absolute paths outside the two repository roots, except approved document
  paths needed for review provenance.

If the extractor cannot parse every selected record or cannot prove the output
passes its secret/PII scanner, it supplies no session narrative and marks the
review incomplete. Raw transcript content is never copied into reports.

### 4.6 Codex execution

The worker runs `codex exec` non-interactively with:

- the existing ChatGPT authentication under `/root/.codex`;
- an empty-by-default environment allowlist containing only variables needed
  for process execution and Codex authentication;
- a read-only sandbox;
- an ephemeral Codex session;
- the temporary review bundle as the only workspace;
- fixed model/effort settings recorded in the report;
- no MCP servers, network-dependent repository tools, project rules, or hooks;
- a bounded wall-clock timeout.

The prompt instructs Codex to review only, cite bundle paths and diff lines,
avoid reproducing secrets, distinguish evidence from inference, and return the
fixed Markdown report format. Model output is untrusted and passes through a
final secret/PII scanner before publication. Unsafe output is replaced by a
failure report.

This follows the official Codex non-interactive review model while adding a
local privacy boundary for the operational Hermes repository.

## 5. Reports and observability

Reports live at:

```text
/var/lib/codex-commit-review/reports/<repository-alias>/<sha>.md
```

The root and repository directories are `0700`; reports are `0600`. Each report
contains:

- repository alias and SHA;
- parent SHA and commit subject;
- review start/end timestamps and duration;
- Codex CLI/model/effort identity;
- sanitized session provenance and references used;
- verdict and findings ordered P0 through P3;
- test gaps and required follow-up evidence;
- execution errors or completeness limitations.

The worker also maintains a non-sensitive status file per repository with the
last reviewed SHA, verdict, timestamp, and report path. Journal messages contain
only repository alias, abbreviated SHA, lifecycle state, verdict, and error
class. They never contain diffs, prompts, model output, identities, or secrets.

No automatic notification into the originating ChatGPT conversation is
assumed. While an interactive monitoring conversation is active, it may read
the status/report files and alert the operator. A future GitHub Action may post
the same review contract on pull requests.

## 6. Failure handling

- Hook failure: commit succeeds; journal records `enqueue_failed`.
- Worker already running: the path unit retriggers after the active run; the
  process lock prevents overlap.
- Codex timeout/auth/network failure: durable `BLOCKED` failure report; queue
  entry completes and can be retried explicitly.
- Commit becomes unreachable after history rewrite: durable `BLOCKED` failure
  report with `commit_unreachable`.
- Repository is dirty or advances during review: irrelevant, because inputs
  come from the exact commit object.
- Service restart: durable queue entries remain and are drained on activation.
- Sanitizer failure: no model invocation; durable `BLOCKED` report.
- Report installation failure: queue entry remains for retry.

## 7. Security boundaries

- Runtime directories and reports are root-only.
- Repository and session paths are fixed/validated; no queue-controlled path
  traversal or command construction is allowed.
- Subprocesses use argument arrays, not shell interpolation.
- The Codex child receives no production application tokens or service
  credentials through its environment.
- The review bundle contains no `.git`, live database, session store, outbox,
  SSH material, `.env`, or unredacted authorization header.
- Review execution has no write access to either repository.
- Installation does not alter Hermes Agent upstream files or operational
  profile behavior.

## 8. Testing strategy

Implementation follows test-driven development.

Unit tests cover:

- enqueue validation, atomic modes, deduplication, and ordering;
- refusal to overwrite an unmanaged hook and idempotent managed installation;
- exact commit and parent selection, including root commits;
- reference routing for Brain and Hermes changes;
- Claude JSONL filtering by role/type/time;
- every redaction category and scanner fail-closed behavior;
- report verdict derivation and unsafe-output rejection;
- timeout, auth failure, unreachable commit, and retry behavior;
- subprocess argument/environment construction with no shell use.

Integration tests use temporary Git repositories and a fake Codex executable
to prove that a real commit enqueues once, the worker reviews the exact SHA,
the commit returns without waiting for review, rapid commits serialize, and a
report is installed with the required permissions. Tests never use live Claude
sessions, live repositories, the real Codex service, or production secrets.

Activation verification then performs one controlled review of the current
Brain HEAD without creating a synthetic commit. It verifies the service/path
units, queue drain, report permissions, report completeness, and absence of
repository changes. The current Hermes dirty working tree must remain byte-for-
byte unchanged.

## 9. Installation and rollback

Installation creates only:

- versioned implementation/tests under `/root/brain/ops/commit-review/`;
- two managed `.git/hooks/post-commit` files;
- `/etc/codex-commit-review.toml` containing no secrets;
- one systemd service and one systemd path unit;
- `/var/lib/codex-commit-review/` runtime directories.

Activation order is implementation/tests, runtime directories, configuration,
systemd units, hooks, path-unit enablement, then controlled review.

Rollback disables the path unit, removes only hooks bearing the managed marker,
removes the installed systemd/config artifacts, reloads systemd, and preserves
reports by default. Removing historical reports is a separate explicit,
destructive action.

## 10. Acceptance criteria

The automation is accepted only when:

1. all unit and integration tests pass;
2. sanitization tests demonstrate RED then GREEN for every required category;
3. hooks are installed without replacing prior unmanaged hooks;
4. a controlled Brain HEAD review produces one durable report;
5. the report names the exact SHA, references, session provenance, and verdict;
6. no secret/PII scanner finding exists in bundle or report;
7. both live repositories are unchanged by review execution;
8. Hermes upstream integrity remains `PASS: BASELINE_VERIFIED`;
9. disabling the path unit stops automatic runs while commits continue to
   succeed;
10. the operator receives the report path and rollback instructions.

## 11. Future GitHub gate

After the local reviewer is stable, the same fixed review instructions and
severity contract may be committed under `.github/` and invoked with
`openai/codex-action@v1` on pull-request events. CI must use repository secrets,
read-only permissions, sanitized/versioned context rather than the local Claude
transcript, and the narrowest sandbox. This is a separate implementation stage
and is not required to activate local per-commit review.
