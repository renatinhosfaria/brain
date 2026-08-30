from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.hermes_integrity import (
    CRITICAL_MANIFEST,
    IntegrityError,
    capture_baseline,
    verify_baseline,
)


class HermesIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "upstream"
        self.baseline = self.root / "baseline.json"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "integrity@example.invalid")
        self.git("config", "user.name", "Integrity Test")
        for index, relative in enumerate(CRITICAL_MANIFEST):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"protected fixture {index}\n".encode())
        self.git("add", ".")
        self.git("commit", "-qm", "initial fixture")

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.strip()

    def assert_reason(self, reason: str, operation) -> None:
        with self.assertRaises(IntegrityError) as raised:
            operation()
        self.assertEqual(raised.exception.reason, reason)

    def test_clean_capture_and_immediate_verify_pass(self) -> None:
        captured = capture_baseline(self.repo, self.baseline)

        self.assertEqual(captured["schema_version"], 1)
        self.assertEqual(verify_baseline(self.repo, self.baseline), captured)

    def test_capture_installs_baseline_with_mode_0600(self) -> None:
        capture_baseline(self.repo, self.baseline)

        self.assertEqual(stat.S_IMODE(self.baseline.stat().st_mode), 0o600)

    def test_capture_rejects_dirty_worktree(self) -> None:
        (self.repo / "untracked.txt").write_text("dirty", encoding="utf-8")

        self.assert_reason(
            "WORKTREE_DIRTY", lambda: capture_baseline(self.repo, self.baseline)
        )
        self.assertFalse(self.baseline.exists())

    def test_verify_rejects_dirty_worktree(self) -> None:
        capture_baseline(self.repo, self.baseline)
        (self.repo / "untracked.txt").write_text("dirty", encoding="utf-8")

        self.assert_reason(
            "WORKTREE_DIRTY", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_changed_protected_file_reports_hash_mismatch(self) -> None:
        capture_baseline(self.repo, self.baseline)
        protected = self.repo / CRITICAL_MANIFEST[0]
        protected.write_text("changed bytes\n", encoding="utf-8")

        self.assert_reason(
            "HASH_MISMATCH", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_deleted_protected_file_fails_closed(self) -> None:
        capture_baseline(self.repo, self.baseline)
        (self.repo / CRITICAL_MANIFEST[0]).unlink()

        self.assert_reason(
            "FILE_MISSING", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_protected_file_replaced_by_symlink_fails_closed(self) -> None:
        capture_baseline(self.repo, self.baseline)
        protected = self.repo / CRITICAL_MANIFEST[0]
        protected.unlink()
        protected.symlink_to(self.repo / CRITICAL_MANIFEST[1])

        self.assert_reason(
            "FILE_SYMLINK", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_new_clean_commit_reports_head_mismatch(self) -> None:
        capture_baseline(self.repo, self.baseline)
        marker = self.repo / "new-version.txt"
        marker.write_text("new head\n", encoding="utf-8")
        self.git("add", "new-version.txt")
        self.git("commit", "-qm", "new head")

        self.assert_reason(
            "HEAD_MISMATCH", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_malformed_baseline_json_is_rejected(self) -> None:
        self.baseline.write_text("{not-json", encoding="utf-8")
        self.baseline.chmod(0o600)

        self.assert_reason(
            "BASELINE_INVALID", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_unsupported_schema_version_is_rejected(self) -> None:
        payload = self.valid_payload()
        payload["schema_version"] = 2
        self.write_payload(payload)

        self.assert_reason(
            "BASELINE_INVALID", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_missing_manifest_entry_is_rejected(self) -> None:
        payload = self.valid_payload()
        del payload["files"][CRITICAL_MANIFEST[0]]
        self.write_payload(payload)

        self.assert_reason(
            "BASELINE_INVALID", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_unexpected_manifest_substitution_is_rejected(self) -> None:
        payload = self.valid_payload()
        entry = payload["files"].pop(CRITICAL_MANIFEST[0])
        payload["files"]["gateway/substitute.py"] = entry
        self.write_payload(payload)

        self.assert_reason(
            "BASELINE_INVALID", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_capture_rejects_non_git_directory(self) -> None:
        non_git = self.root / "not-git"
        non_git.mkdir()

        self.assert_reason(
            "REPO_INVALID", lambda: capture_baseline(non_git, self.baseline)
        )

    def test_repo_path_symlink_is_rejected(self) -> None:
        alias = self.root / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)

        self.assert_reason(
            "REPO_SYMLINK", lambda: capture_baseline(alias, self.baseline)
        )

    def test_capture_refuses_to_overwrite_existing_baseline(self) -> None:
        original = b"operator-owned existing baseline\n"
        self.baseline.write_bytes(original)

        self.assert_reason(
            "BASELINE_EXISTS", lambda: capture_baseline(self.repo, self.baseline)
        )
        self.assertEqual(self.baseline.read_bytes(), original)

    def test_capture_never_creates_output_parent_inside_upstream(self) -> None:
        forbidden_parent = self.repo / "brain-owned-output"

        self.assert_reason(
            "OUTPUT_IN_REPO",
            lambda: capture_baseline(self.repo, forbidden_parent / "baseline.json"),
        )
        self.assertFalse(forbidden_parent.exists())

    def test_verify_does_not_modify_baseline(self) -> None:
        capture_baseline(self.repo, self.baseline)
        before = self.baseline.read_bytes()
        before_stat = self.baseline.stat()

        verify_baseline(self.repo, self.baseline)

        after_stat = self.baseline.stat()
        self.assertEqual(self.baseline.read_bytes(), before)
        self.assertEqual(after_stat.st_ino, before_stat.st_ino)
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)

    def test_verify_rejects_baseline_with_non_private_mode(self) -> None:
        capture_baseline(self.repo, self.baseline)
        self.baseline.chmod(0o644)

        self.assert_reason(
            "BASELINE_INVALID", lambda: verify_baseline(self.repo, self.baseline)
        )

    def test_git_invocations_are_strictly_read_only(self) -> None:
        recorder = self.root / "recording-git"
        record = self.root / "git-arguments.jsonl"
        recorder.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$HERMES_GIT_RECORD"\n'
            'exec /usr/bin/git "$@"\n',
            encoding="utf-8",
        )
        recorder.chmod(0o700)
        previous = os.environ.get("HERMES_GIT_RECORD")
        os.environ["HERMES_GIT_RECORD"] = str(record)
        self.addCleanup(self.restore_environment, "HERMES_GIT_RECORD", previous)

        capture_baseline(self.repo, self.baseline, git_executable=recorder)
        verify_baseline(self.repo, self.baseline, git_executable=recorder)

        invocations = record.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(invocations), 0)
        for invocation in invocations:
            arguments = invocation.split()
            subcommand_index = arguments.index("-C") + 2
            self.assertIn(arguments[subcommand_index], {"rev-parse", "status"})

    def valid_payload(self) -> dict:
        capture_baseline(self.repo, self.baseline)
        payload = json.loads(self.baseline.read_text(encoding="utf-8"))
        self.baseline.unlink()
        return payload

    def write_payload(self, payload: dict) -> None:
        self.baseline.write_text(json.dumps(payload), encoding="utf-8")
        self.baseline.chmod(0o600)

    @staticmethod
    def restore_environment(name: str, previous: str | None) -> None:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


if __name__ == "__main__":
    unittest.main()
