# Automatic Commit Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install a fail-closed, privacy-preserving Codex review after every local commit in `/root/brain` and `/root/.hermes`, with exact-commit evidence, relevant CTWA context, and durable root-only reports.

**Architecture:** Minimal managed `post-commit` hooks atomically enqueue validated repository/SHA pairs. A systemd path unit starts one serialized worker, which builds a sanitized temporary evidence bundle from immutable Git objects and bounded Claude narrative, invokes Codex without repository access, validates the result, and installs a durable report. The implementation lives in Brain; deployed hooks, units, configuration, queue, and reports are generated or installed from versioned assets.

**Tech Stack:** Python 3.11+ standard library (`argparse`, `dataclasses`, `fcntl`, `hashlib`, `json`, `pathlib`, `re`, `subprocess`, `tempfile`, `tomllib`), Git, systemd 255, Codex CLI, `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-30-automatic-commit-review-design.md`

## Global Constraints

- Follow TDD for every behavioral task: add the named failing test, run it and observe the expected failure, implement the smallest production change, then rerun the focused test and the full suite.
- Do not modify `/usr/local/lib/hermes-agent/**`, operational profile behavior, either repository's tracked content outside this plan's Brain paths, or the current dirty Hermes working tree.
- Never pass live repositories, `.git`, raw Claude JSONL, tool results, production credentials, or application environment variables to Codex.
- Use argument-vector subprocess calls with `shell=False`; validate every repository path, SHA, filename, and destination before filesystem or Git operations.
- A missing input, parser error, sanitizer uncertainty, Codex error, timeout, malformed output, or incomplete context must result in a durable `BLOCKED` report.
- Runtime directories are `0700`; queue, status, bundle, and report files are `0600`; atomic files are written with `fsync` plus `os.replace`.
- Each implementation commit must contain only the files named by its task. Never stage the live Hermes changes.

## File and Responsibility Map

| Path | Responsibility |
|---|---|
| `ops/commit-review/commit_review/model.py` | Typed immutable configuration, queue, bundle, invocation, and report records |
| `ops/commit-review/commit_review/config.py` | TOML loading, canonical allowlist validation, safe defaults |
| `ops/commit-review/commit_review/atomic.py` | Root-only directories and durable atomic writes |
| `ops/commit-review/commit_review/queue.py` | Validated enqueue, deduplication, ordered dequeue, lock, retry |
| `ops/commit-review/commit_review/redact.py` | Text redaction, output scanner, binary/text classification |
| `ops/commit-review/commit_review/session_context.py` | Relevant Claude session selection and bounded narrative extraction |
| `ops/commit-review/commit_review/references.py` | Deterministic CTWA reference routing and size accounting |
| `ops/commit-review/commit_review/git_bundle.py` | Exact commit/parent validation and sanitized bundle materialization |
| `ops/commit-review/commit_review/codex_runner.py` | Fixed Codex invocation, environment allowlist, timeout, schema validation |
| `ops/commit-review/commit_review/report.py` | Verdict derivation, report rendering, final scan, status publication |
| `ops/commit-review/commit_review/worker.py` | Serialized queue lifecycle and durable failure handling |
| `ops/commit-review/commit_review/cli.py` | `enqueue`, `worker`, `review`, `retry`, `status`, `install`, `uninstall` commands |
| `ops/commit-review/bin/codex-commit-review` | Stable executable wrapper into the Python package |
| `ops/commit-review/review-prompt.md` | Versioned review contract and evidence/citation rules |
| `ops/commit-review/review-output.schema.json` | Machine-enforced Codex response shape |
| `ops/commit-review/codex-commit-review.toml.example` | Secret-free deployment configuration |
| `ops/commit-review/templates/post-commit` | Managed, non-blocking enqueue hook |
| `ops/commit-review/systemd/*.service`, `*.path` | Serialized worker and queue activation |
| `ops/commit-review/tests/` | Unit, integration, security, and installer tests |
| `ops/commit-review/README.md` | Operator install, status, retry, rollback, and incident procedure |

---

### Task 1: Package skeleton, immutable models, and configuration

**Files:**
- Create: `ops/commit-review/commit_review/__init__.py`
- Create: `ops/commit-review/commit_review/model.py`
- Create: `ops/commit-review/commit_review/config.py`
- Create: `ops/commit-review/codex-commit-review.toml.example`
- Create: `ops/commit-review/tests/__init__.py`
- Create: `ops/commit-review/tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

```python
class ConfigTests(unittest.TestCase):
    def test_loads_only_canonical_allowlisted_repositories(self):
        cfg = load_config(self.write_config("brain = '/root/brain'\nhermes = '/root/.hermes'"))
        self.assertEqual(cfg.repositories, {"brain": Path("/root/brain"), "hermes": Path("/root/.hermes")})

    def test_rejects_relative_unknown_or_duplicate_repository_paths(self):
        for body in ("brain = '../brain'", "other = '/root/brain'", "brain = '/root/.hermes'"):
            with self.subTest(body=body), self.assertRaises(ConfigError):
                load_config(self.write_config(body))

    def test_defaults_are_bounded_and_contain_no_secret_values(self):
        cfg = load_config(self.minimal_config())
        self.assertEqual(cfg.timeout_seconds, 900)
        self.assertLessEqual(cfg.max_bundle_bytes, 8 * 1024 * 1024)
        self.assertEqual(cfg.codex_environment, ("HOME", "PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"))
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `PYTHONPATH=ops/commit-review python3 -m unittest ops.commit-review.tests.test_config` is invalid because of the hyphen; use:

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_config.py' -v
```

Expected: `ModuleNotFoundError: No module named 'commit_review'`.

- [ ] **Step 3: Implement frozen dataclasses and strict TOML loading**

Define `ReviewConfig`, `QueueItem`, `SessionProvenance`, `BundleManifest`, `Finding`, and `ReviewResult` as frozen dataclasses. `load_config(path: Path) -> ReviewConfig` must use `tomllib`, resolve paths without accepting symlink aliases, require exactly the aliases `brain` and `hermes`, and reject unrecognized keys. The example config fixes `/root/brain`, `/root/.hermes`, `/root/.claude/projects/-root`, `/var/lib/codex-commit-review`, the installed CLI path, `gpt-5.6-sol`, `medium`, 900 seconds, 8 MiB bundle size, 240 narrative entries, and 512 KiB narrative bytes.

- [ ] **Step 4: Run focused and full tests**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_config.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add ops/commit-review/commit_review ops/commit-review/tests ops/commit-review/codex-commit-review.toml.example
git commit -m "feat: define commit review configuration"
```

---

### Task 2: Atomic storage and durable queue

**Files:**
- Create: `ops/commit-review/commit_review/atomic.py`
- Create: `ops/commit-review/commit_review/queue.py`
- Create: `ops/commit-review/tests/test_atomic.py`
- Create: `ops/commit-review/tests/test_queue.py`

- [ ] **Step 1: Add RED tests for permissions, validation, order, and deduplication**

```python
def test_enqueue_writes_one_atomic_root_only_item(self):
    item = self.queue.enqueue("brain", self.brain, self.sha)
    files = list(self.queue_dir.glob("*.json"))
    self.assertEqual(len(files), 1)
    self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
    self.assertEqual(json.loads(files[0].read_text())["sha"], self.sha)

def test_duplicate_pending_or_reported_commit_is_not_enqueued(self):
    self.assertTrue(self.queue.enqueue("brain", self.brain, self.sha).created)
    self.assertFalse(self.queue.enqueue("brain", self.brain, self.sha).created)
    self.install_report("brain", self.sha)
    self.assertFalse(self.queue.enqueue("brain", self.brain, self.sha).created)

def test_items_are_claimed_in_creation_order_under_exclusive_lock(self):
    first, second = self.enqueue_two_commits()
    with self.queue.worker_lock():
        self.assertEqual(self.queue.pending(), [first, second])
    with self.assertRaises(WorkerBusy):
        with self.queue.worker_lock(nonblocking=True):
            pass
```

Also cover malformed JSON, extra keys, non-full SHA, alias/path mismatch, symlink queue path, `fsync`/replace failure, retry after a durable report, and preservation of an entry when report installation fails.

- [ ] **Step 2: Run focused tests and confirm missing modules/functions fail**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_atomic.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_queue.py' -v
```

- [ ] **Step 3: Implement storage and queue**

`atomic_write(path, data, mode=0o600)` must create a sibling temporary file with `O_CREAT|O_EXCL|O_NOFOLLOW`, set mode before writing, flush and `fsync` the file, replace the target, then `fsync` the parent directory. Queue filenames are `<20-digit-utc-nanoseconds>-<alias>-<sha>.json`; their JSON schema is exactly `version`, `repository`, `repository_path`, `sha`, `enqueued_at`. Use `fcntl.flock` on `<state>/worker.lock`. A retry explicitly removes only the matching report/status result before enqueuing the same validated pair.

- [ ] **Step 4: Run the full suite and commit**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/commit_review/atomic.py ops/commit-review/commit_review/queue.py ops/commit-review/tests/test_atomic.py ops/commit-review/tests/test_queue.py
git commit -m "feat: add durable commit review queue"
```

---

### Task 3: Fail-closed redaction and scanning

**Files:**
- Create: `ops/commit-review/commit_review/redact.py`
- Create: `ops/commit-review/tests/test_redact.py`
- Create: `ops/commit-review/tests/fixtures/redaction-cases.json`

- [ ] **Step 1: Create table-driven RED tests for every required category**

The fixture contains synthetic examples only, with expected replacement labels for bearer/API credentials, authorization and cookie headers, credential-like assignments, URL userinfo/query strings, emails, Brazilian/international phone forms, WhatsApp JID/LID, `key.id`, `ctwaClid`, `sourceId`, client/message/turn/event IDs, long base64/hex/high-entropy strings, and non-approved absolute paths.

```python
def test_each_sensitive_category_is_replaced_and_scans_clean(self):
    for case in self.cases:
        with self.subTest(case=case["name"]):
            clean = redact_text(case["input"], approved_roots=self.roots)
            self.assertIn(case["replacement"], clean)
            self.assertNotIn(case["secret"], clean)
            self.assertEqual(scan_text(clean), [])

def test_scanner_rejects_residual_or_ambiguous_sensitive_text(self):
    for value in self.residual_values:
        self.assertTrue(scan_text(value))

def test_binary_or_decode_uncertainty_never_returns_text(self):
    self.assertEqual(classify_text(b"a\x00b"), TextKind.BINARY)
    self.assertEqual(classify_text(b"\xff\xfe"), TextKind.BINARY)
```

- [ ] **Step 2: Confirm RED, implement ordered redactors plus independent scanner, then confirm GREEN**

The scanner must not merely rerun the replacement expressions: it independently flags credential keywords with values, identifier labels, phone/JID shapes, URL query/userinfo, email, entropy candidates, NUL/control bytes, and paths outside approved roots. `sanitize_or_raise` returns only scanner-clean UTF-8 or raises `UnsafeContent`.

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_redact.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
```

- [ ] **Step 3: Commit**

```bash
git add ops/commit-review/commit_review/redact.py ops/commit-review/tests/test_redact.py ops/commit-review/tests/fixtures/redaction-cases.json
git commit -m "feat: sanitize commit review evidence"
```

---

### Task 4: Claude narrative selection and extraction

**Files:**
- Create: `ops/commit-review/commit_review/session_context.py`
- Create: `ops/commit-review/tests/test_session_context.py`
- Create: `ops/commit-review/tests/fixtures/claude/brain-session.jsonl`
- Create: `ops/commit-review/tests/fixtures/claude/unrelated-session.jsonl`

- [ ] **Step 1: Add RED tests using fully synthetic JSONL**

```python
def test_selects_latest_relevant_top_level_session(self):
    selected = select_session(self.root, repository="brain", terms=("CTWA", "lifecycle"), cutoff=self.cutoff)
    self.assertEqual(selected.name, "brain-session.jsonl")

def test_extracts_only_user_and_assistant_text_before_commit(self):
    result = extract_narrative(self.session, self.cutoff, max_entries=3, max_bytes=2048, roots=self.roots)
    self.assertEqual([entry.role for entry in result.entries], ["user", "assistant"])
    self.assertNotIn("tool_use", result.text)
    self.assertNotIn("tool_result", result.text)
    self.assertNotIn("after cutoff", result.text)

def test_parse_error_or_unsafe_output_marks_context_incomplete(self):
    self.session.write_text('{bad json}\n')
    result = extract_narrative(self.session, self.cutoff, 3, 2048, self.roots)
    self.assertFalse(result.complete)
    self.assertEqual(result.text, "")
```

Also test exclusion of attachments, sidechains, summaries without a role, nested transcript files, count/byte truncation, timestamp boundaries, deterministic tie-breaking, sanitized provenance without the full filesystem path, and no raw transcript content in error messages.

- [ ] **Step 2: Implement selection/extraction and verify**

Session relevance is calculated from sanitized user/assistant text only. Every top-level candidate must be parsed completely before it is eligible. Provenance stores the basename-derived session ID, covered UTC range, retained count, and omitted count. Any malformed selected session, invalid timestamp, or scanner finding produces an incomplete empty narrative.

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_session_context.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/commit_review/session_context.py ops/commit-review/tests/test_session_context.py ops/commit-review/tests/fixtures/claude
git commit -m "feat: extract sanitized Claude narrative"
```

---

### Task 5: Exact Git evidence and deterministic reference routing

**Files:**
- Create: `ops/commit-review/commit_review/references.py`
- Create: `ops/commit-review/commit_review/git_bundle.py`
- Create: `ops/commit-review/tests/test_references.py`
- Create: `ops/commit-review/tests/test_git_bundle.py`

- [ ] **Step 1: Add RED tests with temporary repositories**

```python
def test_bundle_uses_requested_commit_not_worktree_or_current_head(self):
    reviewed = self.commit("contract.txt", "committed\n")
    later = self.commit("contract.txt", "later\n")
    Path(self.repo, "contract.txt").write_text("dirty secret\n")
    bundle = build_git_evidence(self.repo, reviewed, self.bundle, self.redactor)
    self.assertIn("committed", bundle.changed_files_text)
    self.assertNotIn("later", bundle.changed_files_text)
    self.assertNotIn("dirty secret", bundle.changed_files_text)

def test_root_commit_uses_empty_tree_parent(self):
    root = self.commit("first.txt", "first\n")
    evidence = inspect_commit(self.repo, root)
    self.assertIsNone(evidence.parent_sha)
    self.assertIn("first.txt", evidence.changed_paths)

def test_route_lifecycle_change_includes_required_contracts(self):
    routed = route_references("brain", ["src/lifecycle_writer.py"], "lifecycle writer rollout")
    self.assertIn("docs/superpowers/specs/2026-08-29-ctwa-brain-lifecycle-design.md", routed.required)
    self.assertIn("docs/superpowers/plans/2026-08-29-ctwa-lifecycle-writer-rollout.md", routed.required)
```

Cover annotated tags/non-commit objects, unreachable commits, merge commits using first parent with all-parent metadata, rename/delete, binary metadata only, symlinks as metadata only, submodules as metadata only, oversized files, sanitization failure, uncertain routing fallback to all bounded CTWA plans, required-reference truncation, and deterministic manifest ordering.

- [ ] **Step 2: Implement safe Git reads and routing**

Use `git -C <canonical repo> cat-file -e <sha>^{commit}`, `show -s --format=...`, `diff-tree --root --no-commit-id -r -M --raw`, `diff --no-ext-diff --no-textconv`, and `cat-file blob <sha>:<path>` via argument arrays. Never checkout. Set `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, and explicit safe Git config arguments; inspect blob size before reading. Materialized filenames are manifest-assigned hashes rather than source paths, preventing traversal and newline ambiguity. Reference blobs must also be read from explicit committed revisions: reviewed SHA for same-repo references and the captured start SHA for cross-repo references. Write `manifest.json`, `commit.md`, `diff.patch`, `changed/`, `references/`, and later `session.md` only through sanitized atomic writes. If required evidence exceeds the limit, create an incomplete manifest and do not invoke Codex.

- [ ] **Step 3: Verify and commit**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_references.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_git_bundle.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/commit_review/references.py ops/commit-review/commit_review/git_bundle.py ops/commit-review/tests/test_references.py ops/commit-review/tests/test_git_bundle.py
git commit -m "feat: build exact sanitized commit bundles"
```

---

### Task 6: Fixed review contract and isolated Codex invocation

**Files:**
- Create: `ops/commit-review/review-prompt.md`
- Create: `ops/commit-review/review-output.schema.json`
- Create: `ops/commit-review/commit_review/codex_runner.py`
- Create: `ops/commit-review/tests/test_codex_runner.py`
- Create: `ops/commit-review/tests/fixtures/fake_codex.py`

- [ ] **Step 1: Add RED tests for command, environment, timeout, and schema**

```python
def test_command_is_ephemeral_read_only_and_ignores_user_rules(self):
    invocation = build_invocation(self.cfg, self.bundle, self.output)
    self.assertEqual(invocation.cwd, self.bundle)
    self.assertIn("--ephemeral", invocation.argv)
    self.assertIn("--ignore-user-config", invocation.argv)
    self.assertIn("--ignore-rules", invocation.argv)
    self.assertIn("read-only", invocation.argv)
    self.assertNotIn(str(self.brain), " ".join(invocation.argv))

def test_environment_contains_allowlist_and_no_application_credentials(self):
    env = build_environment(self.cfg, self.host_env)
    self.assertEqual(set(env), {"HOME", "PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"})
    self.assertNotIn("OPENAI_API_KEY", env)
    self.assertNotIn("DATABASE_URL", env)

def test_timeout_nonzero_exit_and_bad_json_are_incomplete(self):
    for mode in ("timeout", "exit-1", "invalid-json", "invalid-schema"):
        with self.subTest(mode=mode):
            self.assertFalse(self.run_fake(mode).complete)
```

- [ ] **Step 2: Define the schema and prompt**

The JSON schema requires `verdict`, `summary`, `findings`, `test_gaps`, `follow_up_evidence`, and `limitations`; forbids additional properties; restricts verdict to the three specified values and severity to P0-P3. The prompt lists all nine review dimensions from the spec, requires citations to bundle-relative files and diff line ranges, bans secret reproduction, distinguishes evidence from inference, and directs any missing required evidence to `BLOCKED`.

- [ ] **Step 3: Implement `subprocess.run` without a shell and verify**

Invoke the configured executable with `exec --ephemeral --ignore-user-config --ignore-rules --sandbox read-only --model <model> --config model_reasoning_effort=<effort> --output-schema <schema> -o <output> -`. Set `cwd` to the bundle, `stdin` to the fixed prompt, `start_new_session=True`, and the bounded timeout. Do not enable MCP/network features. Record CLI version separately with the same environment. Validate JSON and semantic verdict consistency before returning `ReviewResult`.

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_codex_runner.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/review-prompt.md ops/commit-review/review-output.schema.json ops/commit-review/commit_review/codex_runner.py ops/commit-review/tests/test_codex_runner.py ops/commit-review/tests/fixtures/fake_codex.py
git commit -m "feat: run isolated Codex commit reviews"
```

---

### Task 7: Reports, verdict enforcement, and worker lifecycle

**Files:**
- Create: `ops/commit-review/commit_review/report.py`
- Create: `ops/commit-review/commit_review/worker.py`
- Create: `ops/commit-review/tests/test_report.py`
- Create: `ops/commit-review/tests/test_worker.py`

- [ ] **Step 1: Add RED tests for success and every fail-closed path**

```python
def test_p2_forces_blocked_even_if_model_says_approved(self):
    rendered = render_report(self.result(verdict="APPROVED", findings=[self.finding("P2")]))
    self.assertIn("Verdict: BLOCKED", rendered)

def test_worker_removes_entry_only_after_durable_report(self):
    self.worker.process_one(self.item)
    self.assertTrue(self.report_path.exists())
    self.assertFalse(self.item.path.exists())
    self.assertEqual(stat.S_IMODE(self.report_path.stat().st_mode), 0o600)

def test_failure_classes_install_blocked_report_without_sensitive_detail(self):
    for failure in self.failures:
        with self.subTest(failure=failure.code):
            path = self.worker.process_failure(self.item, failure)
            self.assertIn("Verdict: BLOCKED", path.read_text())
            self.assertIn(failure.code, path.read_text())
            self.assertNotIn(failure.raw_detail, path.read_text())
```

Cover no findings, P3-only, P0/P1/P2, incomplete context, unreachable SHA, scanner failure before invocation, auth failure, timeout, malformed output, unsafe model output, temp cleanup, status JSON fields, repeated worker invocation, FIFO drain, and report installation failure retaining the queue item.

- [ ] **Step 2: Implement deterministic report/status publication and worker orchestration**

The worker snapshots both repository HEADs at start, creates a `TemporaryDirectory` under the root-only runtime tmp directory, builds and scans evidence, invokes Codex only when complete, scans model JSON and rendered Markdown, writes `<reports>/<alias>/<sha>.md`, writes `<status>/<alias>.json`, then deletes the queue entry. A failure report contains only the safe failure class and operator action. Journal output uses `codex-commit-review repository=<alias> sha=<12> state=<state> verdict=<verdict> error=<class>`.

- [ ] **Step 3: Verify and commit**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_report.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_worker.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/commit_review/report.py ops/commit-review/commit_review/worker.py ops/commit-review/tests/test_report.py ops/commit-review/tests/test_worker.py
git commit -m "feat: publish fail-closed review reports"
```

---

### Task 8: CLI, managed hook, installer, and systemd assets

**Files:**
- Create: `ops/commit-review/commit_review/cli.py`
- Create: `ops/commit-review/bin/codex-commit-review`
- Create: `ops/commit-review/templates/post-commit`
- Create: `ops/commit-review/systemd/codex-commit-review.service`
- Create: `ops/commit-review/systemd/codex-commit-review.path`
- Create: `ops/commit-review/tests/test_cli.py`
- Create: `ops/commit-review/tests/test_installer.py`
- Create: `ops/commit-review/tests/test_systemd_assets.py`

- [ ] **Step 1: Add RED tests for hooks and deployment safety**

```python
def test_hook_enqueues_head_and_always_returns_success(self):
    result = self.run_hook(fake_cli_exit=73)
    self.assertEqual(result.returncode, 0)
    self.assertEqual(self.fake_cli_args(), ["enqueue", "--repository", "brain", "--sha", self.head])

def test_install_refuses_unmanaged_hook_and_is_idempotent_for_managed_hook(self):
    self.hook.write_text("#!/bin/sh\necho existing\n")
    with self.assertRaises(UnmanagedHookError):
        install_hook(self.repo, "brain", self.cli)
    self.hook.unlink()
    install_hook(self.repo, "brain", self.cli)
    first = self.hook.read_bytes()
    install_hook(self.repo, "brain", self.cli)
    self.assertEqual(self.hook.read_bytes(), first)

def test_uninstall_removes_only_managed_artifacts_and_preserves_reports(self):
    self.install_all()
    uninstall(self.layout)
    self.assertFalse(self.managed_hook.exists())
    self.assertTrue(self.unmanaged_hook.exists())
    self.assertTrue(self.report.exists())
```

Also test linked worktree hook resolution, detached HEAD, bad SHA, CLI status/retry/review, exact unit paths, `UMask=0077`, service hardening, no production environment files, path trigger, and executable permissions.

- [ ] **Step 2: Implement CLI and assets**

The hook has marker `# managed-by: codex-commit-review/v1`, resolves `git rev-parse --show-toplevel` and `git rev-parse HEAD`, invokes only `/usr/local/bin/codex-commit-review enqueue ...`, emits one safe `logger` event on failure, and exits zero. The service runs as root with `Type=oneshot`, `UMask=0077`, `PrivateTmp=yes`, `ProtectSystem=strict`, explicit read-only repository/session/Codex paths, explicit writable runtime path, `NoNewPrivileges=yes`, and no application `EnvironmentFile`. The path unit watches `/var/lib/codex-commit-review/queue`.

The installer command accepts `--dry-run`; validates all source and destination files; creates runtime paths; copies the wrapper/package/prompt/schema to `/opt/codex-commit-review`; writes `/etc/codex-commit-review.toml`; copies units; reloads systemd; installs both hooks; and enables only the path unit. On any preflight error it changes nothing. Uninstall disables the path unit, removes managed hooks and installed code/config/units, reloads systemd, and preserves runtime reports.

- [ ] **Step 3: Verify and commit**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_cli.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_installer.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_systemd_assets.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/commit_review/cli.py ops/commit-review/bin ops/commit-review/templates ops/commit-review/systemd ops/commit-review/tests/test_cli.py ops/commit-review/tests/test_installer.py ops/commit-review/tests/test_systemd_assets.py
git commit -m "feat: install automatic commit review service"
```

---

### Task 9: End-to-end integration and adversarial security tests

**Files:**
- Create: `ops/commit-review/tests/test_integration.py`
- Create: `ops/commit-review/tests/test_security.py`
- Create: `ops/commit-review/tests/fixtures/fake_logger.py`

- [ ] **Step 1: Build an isolated deployment harness and add RED scenarios**

```python
def test_commit_returns_before_fake_review_finishes_and_report_targets_exact_sha(self):
    started = time.monotonic()
    sha = self.git_commit("change.txt", "safe change\n")
    self.assertLess(time.monotonic() - started, 1.0)
    self.run_worker()
    report = self.report_for(sha).read_text()
    self.assertIn(f"Commit: {sha}", report)

def test_rapid_commits_are_serialized_and_each_reviewed_once(self):
    shas = [self.git_commit(f"{n}.txt", f"{n}\n") for n in range(3)]
    self.run_two_workers_concurrently()
    self.assertEqual(self.fake_codex_reviewed_shas(), shas)

def test_live_repositories_and_raw_session_are_inaccessible_to_fake_codex(self):
    self.run_review()
    invocation = self.fake_codex_invocation()
    self.assertEqual(invocation["cwd"], self.bundle_path)
    self.assertFalse(Path(invocation["cwd"], ".git").exists())
    self.assertNotIn("DATABASE_URL", invocation["env"])
```

The harness uses two temporary Git repositories, a synthetic Claude directory, a temporary runtime root, fake Codex/logger executables, and installer destination overrides that are available only under `CODEX_COMMIT_REVIEW_TESTING=1`. It never reads live sessions or invokes real Codex/systemd.

- [ ] **Step 2: Add malicious-input cases**

Test path traversal in alias/SHA/filenames, queue symlinks, malicious commit subjects, filenames containing spaces/newlines/leading dashes, Git config external diff/textconv, prompt injection in changed files/session/references, secret values split across narrative blocks, oversized decompression-like blobs, report symlink attacks, inherited environment leakage, and cleanup after forced termination. Assert no writes outside the harness and `BLOCKED` for any unresolved ambiguity.

- [ ] **Step 3: Implement only the integration fixes exposed by RED tests, run all tests, and commit**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_integration.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -p 'test_security.py' -v
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git add ops/commit-review/tests/test_integration.py ops/commit-review/tests/test_security.py ops/commit-review/tests/fixtures/fake_logger.py ops/commit-review/commit_review
git commit -m "test: prove automatic commit review end to end"
```

Expected full-suite result: zero failures/errors/skips. Record the test count in the implementation handoff.

---

### Task 10: Operator documentation and pre-activation audit

**Files:**
- Create: `ops/commit-review/README.md`
- Modify only if evidence requires correction: files created in Tasks 1-9

- [ ] **Step 1: Write documentation tests before the document**

Extend `test_installer.py` to assert the README contains exact commands for dry-run, install, status, manual exact-SHA review, retry, journal inspection, disable, enable, uninstall, and report preservation. Assert it warns that commits are not blocked and that `BLOCKED` reports require operator action.

- [ ] **Step 2: Write the runbook**

Document these exact operator flows:

```bash
sudo ops/commit-review/bin/codex-commit-review install --dry-run
sudo ops/commit-review/bin/codex-commit-review install
sudo /usr/local/bin/codex-commit-review status
sudo /usr/local/bin/codex-commit-review review --repository brain --sha "$(git rev-parse HEAD)"
brain_sha="$(git -C /root/brain rev-parse HEAD)"
sudo /usr/local/bin/codex-commit-review retry --repository brain --sha "$brain_sha"
sudo journalctl -u codex-commit-review.service --since today
sudo systemctl disable --now codex-commit-review.path
sudo systemctl enable --now codex-commit-review.path
sudo /usr/local/bin/codex-commit-review uninstall
```

Explain the report schema, P0-P3/verdict policy, privacy boundary, failure classes, queue recovery, unmanaged-hook refusal, preserved reports, and separate destructive report deletion.

- [ ] **Step 3: Run pre-activation checks and commit docs**

```bash
PYTHONPATH=ops/commit-review python3 -m unittest discover -s ops/commit-review/tests -v
git diff --check
git status --short
git add ops/commit-review/README.md ops/commit-review/tests/test_installer.py
git commit -m "docs: add commit review operations runbook"
```

Expected before commit: only the README and its test are modified. Expected after commit: clean Brain worktree.

---

### Task 11: Controlled live installation and acceptance verification

**Files:**
- Install generated copies under: `/opt/codex-commit-review/`, `/usr/local/bin/codex-commit-review`, `/etc/codex-commit-review.toml`, `/etc/systemd/system/codex-commit-review.{service,path}`
- Install managed hooks under: `/root/brain/.git/hooks/post-commit`, `/root/.hermes/.git/hooks/post-commit`
- Create runtime state under: `/var/lib/codex-commit-review/`
- Do not modify tracked repository files

- [ ] **Step 1: Capture immutable pre-install evidence**

```bash
git -C /root/brain status --porcelain=v1 > /tmp/codex-review-brain-before
git -C /root/.hermes status --porcelain=v1 > /tmp/codex-review-hermes-before
git -C /root/.hermes diff --binary > /tmp/codex-review-hermes-before.diff
git -C /root/brain rev-parse HEAD
git -C /root/.hermes rev-parse HEAD
```

Record both HEADs in the handoff. The temporary evidence contains working-tree data and must remain root-only (`chmod 600`) and be deleted after comparison.

- [ ] **Step 2: Run dry-run and inspect exact target set**

```bash
sudo /root/brain/ops/commit-review/bin/codex-commit-review install --dry-run
```

Expected: only the paths listed above plus `/var/lib/codex-commit-review/**`; abort on either unmanaged hook or any unexpected target.

- [ ] **Step 3: Install and validate permissions/units without creating a commit**

```bash
sudo /root/brain/ops/commit-review/bin/codex-commit-review install
systemctl is-enabled codex-commit-review.path
systemctl is-active codex-commit-review.path
systemd-analyze verify /etc/systemd/system/codex-commit-review.service /etc/systemd/system/codex-commit-review.path
stat -c '%a %n' /var/lib/codex-commit-review /var/lib/codex-commit-review/queue /var/lib/codex-commit-review/reports
```

Expected: path unit `enabled` and `active`; unit verification exits zero; runtime directories report `700`.

- [ ] **Step 4: Run one controlled review of the current Brain HEAD**

```bash
brain_sha="$(git -C /root/brain rev-parse HEAD)"
sudo /usr/local/bin/codex-commit-review review --repository brain --sha "$brain_sha"
sudo /usr/local/bin/codex-commit-review status --repository brain
sudo test -f "/var/lib/codex-commit-review/reports/brain/$brain_sha.md"
sudo stat -c '%a' "/var/lib/codex-commit-review/reports/brain/$brain_sha.md"
```

Expected: queue drains, status names the exact SHA and one of the three verdicts, report mode is `600`, and the report includes references, sanitized session provenance, timings, model/effort, findings, test gaps, follow-up evidence, and limitations. A `BLOCKED` verdict is an operationally valid review result but must be investigated before accepting the automation.

- [ ] **Step 5: Verify privacy, repository immutability, and Hermes integrity**

```bash
sudo /usr/local/bin/codex-commit-review audit-report --repository brain --sha "$brain_sha"
diff -u /tmp/codex-review-brain-before <(git -C /root/brain status --porcelain=v1)
diff -u /tmp/codex-review-hermes-before <(git -C /root/.hermes status --porcelain=v1)
git -C /root/.hermes diff --binary > /tmp/codex-review-hermes-after.diff
cmp --silent /tmp/codex-review-hermes-before.diff /tmp/codex-review-hermes-after.diff
cd /root/.hermes && ./.venv/bin/python ops/hermes-team/verify_team.py
```

Expected: report audit `PASS`, both status comparisons empty, Hermes binary diff comparison exits zero, and integrity output includes `PASS: BASELINE_VERIFIED`.

- [ ] **Step 6: Prove disable/enable behavior without a synthetic commit**

```bash
sudo systemctl disable --now codex-commit-review.path
systemctl is-enabled codex-commit-review.path
sudo systemctl enable --now codex-commit-review.path
systemctl is-enabled codex-commit-review.path
systemctl is-active codex-commit-review.path
```

Expected sequence: `disabled`, then `enabled`, then `active`. Hook execution itself is already proven in the isolated integration harness, so live verification does not create noise commits.

- [ ] **Step 7: Remove sensitive temporary evidence and publish handoff**

Delete only these exact temporary files after all comparisons succeed:

```bash
rm /tmp/codex-review-brain-before /tmp/codex-review-hermes-before /tmp/codex-review-hermes-before.diff /tmp/codex-review-hermes-after.diff
```

The final handoff records test count, both captured HEADs, unit status, controlled report path/verdict, privacy audit, repository immutability, Hermes baseline result, and rollback command. Do not claim acceptance if any required check is missing or if the controlled report is incomplete.

---

## Rollback Checkpoint

If activation exposes a defect, stop automation while preserving evidence:

```bash
sudo systemctl disable --now codex-commit-review.path
sudo /usr/local/bin/codex-commit-review uninstall
```

Then verify both managed hooks and installed unit/config/code paths are absent, commits still work, repositories retain their exact pre-uninstall state, and `/var/lib/codex-commit-review/reports/` remains present. Historical report deletion is outside this plan and requires a separate explicit destructive request.
