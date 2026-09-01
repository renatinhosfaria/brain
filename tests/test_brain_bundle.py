from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

spec = importlib.util.spec_from_file_location("brain_bundle", SCRIPTS / "brain_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)

GOOD_CONFIG = """
[principals.default]
mode = "gateway"
token_sha256 = "a1"
tools = ["conversation_context"]
"""


class _Fixture:
    """Shared setup only. Not a TestCase, so nothing here is inherited
    into every suite and run again under a different name."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "bundles"
        self.root.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.config = self.base / "brain.toml"
        self.config.write_text(GOOD_CONFIG, encoding="utf-8")

        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        plugin = self.repo / "integrations/hermes/brain-ceo-bridge"
        plugin.mkdir(parents=True)
        (plugin / "tools.py").write_text("# fake tools\n", encoding="utf-8")
        (plugin / "__init__.py").write_text("# fake init\n", encoding="utf-8")
        (self.repo / "file.txt").write_text("one", encoding="utf-8")
        self.commit("first")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def commit(self, message: str) -> str:
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", message], check=True
        )
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def create(self) -> Path:
        return bundle.create(self.root, self.repo, self.config)


class BundleTests(_Fixture, unittest.TestCase):
    def test_identity_is_the_full_sha(self) -> None:
        """An abbreviation can collide; a slot must name exactly one tree."""
        created = self.create()

        self.assertEqual(len(created.name), 40)
        self.assertEqual((self.root / "candidate").resolve(), created)

    def test_a_dirty_worktree_is_refused(self) -> None:
        (self.repo / "file.txt").write_text("uncommitted", encoding="utf-8")

        with self.assertRaisesRegex(bundle.BundleError, "dirty"):
            self.create()

    def test_an_untracked_file_also_counts_as_dirty(self) -> None:
        (self.repo / "stray.txt").write_text("x", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            self.create()

    def test_a_config_granting_more_than_context_is_refused(self) -> None:
        self.config.write_text(
            GOOD_CONFIG.replace(
                '["conversation_context"]', '["conversation_context", "turn_register"]'
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(bundle.BundleError, "turn_register"):
            self.create()

    def test_a_writer_principal_is_refused(self) -> None:
        self.config.write_text(
            GOOD_CONFIG + '\n[principals.writer]\nmode = "service"\ntools = []\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(bundle.BundleError, "writer"):
            self.create()

    def test_the_plugin_comes_from_the_named_repo(self) -> None:
        """--repo must control the plugin source, not just the git metadata.

        A test that silently copies /root/brain's plugin is asserting against
        the developer's working tree, not against the bundle it built.
        """
        created = self.create()

        self.assertEqual(
            (created / "plugin" / "tools.py").read_text(encoding="utf-8"),
            "# fake tools\n",
        )
        self.assertTrue((created / "plugin" / "__init__.py").is_file())
        self.assertEqual(
            (created / "brain.toml").read_text(encoding="utf-8"), GOOD_CONFIG
        )

    def test_verify_accepts_an_untouched_bundle(self) -> None:
        created = self.create()

        self.assertEqual(bundle.verify(self.root, "candidate"), created.name)

    def test_verify_detects_a_single_changed_byte(self) -> None:
        created = self.create()
        target = created / "plugin" / "tools.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with self.assertRaisesRegex(bundle.BundleError, "changed since creation"):
            bundle.verify(self.root, "candidate")

    def test_verify_detects_an_added_file(self) -> None:
        created = self.create()
        (created / "plugin" / "extra.py").write_text("x", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            bundle.verify(self.root, "candidate")

    def test_verify_detects_a_removed_file(self) -> None:
        created = self.create()
        (created / "brain.toml").unlink()

        with self.assertRaises(bundle.BundleError):
            bundle.verify(self.root, "candidate")

    # ------------------------------------------------------------------

    def test_the_first_promotion_leaves_previous_unset(self) -> None:
        """There is no earlier bundle to fall back to, and it must show."""
        self.create()

        active, previous = bundle.promote(self.root)

        self.assertEqual((self.root / "active").resolve().name, active)
        self.assertIsNone(previous)
        self.assertFalse((self.root / "previous").is_symlink())

    def test_rotation_moves_active_into_previous(self) -> None:
        first = self.create()
        bundle.promote(self.root)
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        second = self.create()

        active, previous = bundle.promote(self.root)

        self.assertEqual(active, second.name)
        self.assertEqual(previous, first.name)
        self.assertEqual((self.root / "active").resolve().name, second.name)
        self.assertEqual((self.root / "previous").resolve().name, first.name)

    def test_promoting_the_active_bundle_again_is_refused(self) -> None:
        self.create()
        bundle.promote(self.root)

        with self.assertRaisesRegex(bundle.BundleError, "already active"):
            bundle.promote(self.root)

    def test_promotion_verifies_before_rotating(self) -> None:
        """A corrupt candidate must not displace a good active bundle."""
        first = self.create()
        bundle.promote(self.root)
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        second = self.create()
        (second / "brain.toml").write_text("tampered", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            bundle.promote(self.root)

        self.assertEqual((self.root / "active").resolve().name, first.name)
        self.assertFalse((self.root / "previous").is_symlink())

    def test_status_reports_every_slot_and_flags_corruption(self) -> None:
        created = self.create()
        bundle.promote(self.root)
        (created / "brain.toml").write_text("tampered", encoding="utf-8")

        rows = {slot: state for slot, _, state in bundle.status(self.root)}

        self.assertIn("INVALID", rows["active"])
        self.assertEqual(rows["previous"], "unset")

    def test_manifest_records_the_commit_it_was_built_from(self) -> None:
        created = self.create()

        recorded = json.loads((created / "MANIFEST.json").read_text(encoding="utf-8"))

        self.assertEqual(recorded["commit"], created.name)
        self.assertIn("plugin/tools.py", recorded["files"])


class BundleImmutabilityTests(_Fixture, unittest.TestCase):
    """A bundle names a commit; its contents may never be rewritten."""

    def test_rebuilding_an_identical_bundle_is_accepted(self) -> None:
        first = self.create()

        again = self.create()

        self.assertEqual(again, first)
        self.assertEqual(bundle.verify(self.root, "candidate"), first.name)

    def test_rebuilding_with_different_content_is_refused(self) -> None:
        """The config can change under a commit; the bundle must not."""
        self.create()
        self.config.write_text(
            GOOD_CONFIG.replace('token_sha256 = "a1"', 'token_sha256 = "b2"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(bundle.BundleError, "differs"):
            self.create()

    def test_a_rebuild_never_destroys_an_existing_bundle(self) -> None:
        first = self.create()
        marker = (first / "brain.toml").read_text(encoding="utf-8")
        self.config.write_text(
            GOOD_CONFIG.replace('token_sha256 = "a1"', 'token_sha256 = "b2"'),
            encoding="utf-8",
        )

        with self.assertRaises(bundle.BundleError):
            self.create()

        self.assertEqual((first / "brain.toml").read_text(encoding="utf-8"), marker)
        self.assertEqual(bundle.verify(self.root, "candidate"), first.name)

    def test_accepting_an_existing_bundle_routes_through_verify(self) -> None:
        """Acceptance must be a verification, not only a comparison.

        Content equality implies the manifest agrees today, but the acceptance
        path is where "this bundle is good" is asserted, and it should say so
        through the same function every other caller trusts.
        """
        self.create()
        checked = []
        real_verify = bundle.verify

        def watched(root, ref):
            checked.append(ref)
            return real_verify(root, ref)

        bundle.verify = watched
        try:
            self.create()
        finally:
            bundle.verify = real_verify

        self.assertTrue(checked, "create accepted an existing bundle unverified")

    def test_a_corrupt_existing_bundle_is_not_silently_repaired(self) -> None:
        first = self.create()
        (first / "brain.toml").write_text("tampered", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            self.create()


class SlotAtomicityTests(_Fixture, unittest.TestCase):
    def test_repointing_never_leaves_the_slot_missing(self) -> None:
        """A slot that vanishes mid-swap is a slot nobody can trust."""
        first = self.create()
        bundle.promote(self.root)
        seen = []
        real_replace = bundle.os.replace

        def watched(src, dst):
            seen.append((self.root / "active").is_symlink())
            return real_replace(src, dst)

        bundle.os.replace = watched
        try:
            bundle._point(self.root, "active", first.name)
        finally:
            bundle.os.replace = real_replace

        self.assertTrue(seen and all(seen), "slot was absent during the swap")
        self.assertEqual((self.root / "active").resolve().name, first.name)


class AuthoritativeStateTests(_Fixture, unittest.TestCase):
    """active and previous are one state, changed by one atomic write."""

    def second_bundle(self) -> Path:
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        return self.create()

    def test_state_lives_in_one_file(self) -> None:
        first = self.create()
        bundle.promote(self.root)

        state = bundle.read_state(self.root)

        self.assertEqual(state["active"], first.name)
        self.assertIsNone(state["previous"])
        self.assertTrue((self.root / "slots.json").is_file())

    def test_symlinks_are_a_view_of_the_state(self) -> None:
        first = self.create()
        bundle.promote(self.root)
        (self.root / "active").unlink()

        bundle.refresh_views(self.root)

        self.assertEqual((self.root / "active").resolve().name, first.name)

    def test_the_state_file_is_authoritative_over_the_symlinks(self) -> None:
        """A tampered view must never change what the tool believes."""
        first = self.create()
        bundle.promote(self.root)
        second = self.second_bundle()
        (self.root / "active").unlink()
        (self.root / "active").symlink_to(second.name)

        self.assertEqual(bundle.read_state(self.root)["active"], first.name)

    def test_a_rotation_is_a_single_write(self) -> None:
        """Two sequential updates can be interrupted between them."""
        self.create()
        bundle.promote(self.root)
        self.second_bundle()
        writes = []
        real_replace = bundle.os.replace

        def counted(src, dst):
            if Path(dst).name == "slots.json":
                writes.append(Path(dst).name)
            return real_replace(src, dst)

        bundle.os.replace = counted
        try:
            bundle.promote(self.root)
        finally:
            bundle.os.replace = real_replace

        self.assertEqual(len(writes), 1, "state must change in exactly one write")

    def test_an_interrupted_state_write_leaves_the_old_state(self) -> None:
        first = self.create()
        bundle.promote(self.root)
        second = self.second_bundle()
        real_replace = bundle.os.replace

        def crash(src, dst):
            if Path(dst).name == "slots.json":
                raise OSError("interrupted mid-rotation")
            return real_replace(src, dst)

        bundle.os.replace = crash
        try:
            with self.assertRaises(OSError):
                bundle.promote(self.root)
        finally:
            bundle.os.replace = real_replace

        state = bundle.read_state(self.root)
        self.assertEqual(state["active"], first.name)
        self.assertIsNone(state["previous"])
        self.assertNotEqual(state["active"], second.name)

    def test_active_and_previous_are_never_the_same_bundle(self) -> None:
        self.create()
        bundle.promote(self.root)
        self.second_bundle()
        bundle.promote(self.root)

        state = bundle.read_state(self.root)

        self.assertNotEqual(state["active"], state["previous"])


class RollbackTransactionTests(_Fixture, unittest.TestCase):
    """A plan is a promise about a specific state, not a suggestion."""

    def release(self, name: str) -> Path:
        (self.repo / "file.txt").write_text(name, encoding="utf-8")
        self.commit(name)
        created = self.create()
        bundle.promote(self.root)
        return created

    def two_releases(self) -> tuple[Path, Path]:
        first = self.create()
        bundle.promote(self.root)
        return first, self.release("second")

    def test_planning_verifies_previous_and_changes_nothing(self) -> None:
        first, second = self.two_releases()
        before = bundle.read_state(self.root)

        plan = bundle.plan_rollback(self.root)

        self.assertEqual(plan["target"], first.name)
        self.assertEqual(plan["expected_active"], second.name)
        self.assertEqual(plan["revision"], before["revision"])
        self.assertEqual(bundle.read_state(self.root), before)

    def test_planning_refuses_an_unverifiable_previous(self) -> None:
        first, _ = self.two_releases()
        (first / "brain.toml").write_text("tampered", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            bundle.plan_rollback(self.root)

    def test_a_failed_validation_leaves_the_last_validated_deployment(self) -> None:
        _, second = self.two_releases()

        bundle.plan_rollback(self.root)

        self.assertEqual(bundle.read_state(self.root)["active"], second.name)

    def test_recording_the_plan_swaps_active_and_previous(self) -> None:
        first, second = self.two_releases()

        plan = bundle.plan_rollback(self.root)
        restored, displaced = bundle.record_rollback(self.root, plan)

        self.assertEqual((restored, displaced), (first.name, second.name))
        state = bundle.read_state(self.root)
        self.assertEqual(state["active"], first.name)
        self.assertEqual(state["previous"], second.name)

    def test_a_promote_between_plan_and_record_invalidates_the_plan(self) -> None:
        """The reported scenario: the plan describes a state that has moved.

        Recording it anyway would leave slots.json naming B while the machine
        actually runs A, and nothing would say so.
        """
        first, second = self.two_releases()
        plan = bundle.plan_rollback(self.root)
        self.assertEqual((plan["target"], plan["expected_active"]),
                         (first.name, second.name))

        third = self.release("third")
        before = bundle.read_state(self.root)
        self.assertEqual((before["active"], before["previous"]),
                         (third.name, second.name))

        with self.assertRaisesRegex(bundle.BundleError, "changed since"):
            bundle.record_rollback(self.root, plan)

        self.assertEqual(bundle.read_state(self.root), before)

    def test_a_stale_revision_alone_is_enough_to_refuse(self) -> None:
        self.two_releases()
        plan = bundle.plan_rollback(self.root)
        stale = {**plan, "revision": plan["revision"] - 1}

        with self.assertRaisesRegex(bundle.BundleError, "changed since"):
            bundle.record_rollback(self.root, stale)

    def test_a_forged_expected_active_is_refused(self) -> None:
        first, _ = self.two_releases()
        plan = bundle.plan_rollback(self.root)

        with self.assertRaises(bundle.BundleError):
            bundle.record_rollback(self.root, {**plan, "expected_active": first.name})

    def test_a_forged_target_is_refused(self) -> None:
        _, second = self.two_releases()
        plan = bundle.plan_rollback(self.root)

        with self.assertRaises(bundle.BundleError):
            bundle.record_rollback(self.root, {**plan, "target": second.name})

    def test_recording_without_a_previous_bundle_is_refused(self) -> None:
        self.create()
        bundle.promote(self.root)

        with self.assertRaisesRegex(bundle.BundleError, "no previous"):
            bundle.plan_rollback(self.root)


class StateInvariantTests(_Fixture, unittest.TestCase):
    def test_active_and_previous_may_never_be_equal(self) -> None:
        first = self.create()
        bundle.promote(self.root)

        with self.assertRaisesRegex(bundle.BundleError, "same bundle"):
            bundle.write_state(
                self.root,
                {"candidate": first.name, "active": first.name,
                 "previous": first.name, "revision": 9},
            )

    def test_a_short_sha_is_refused(self) -> None:
        first = self.create()

        with self.assertRaisesRegex(bundle.BundleError, "full"):
            bundle.write_state(
                self.root,
                {"candidate": first.name, "active": first.name[:12],
                 "previous": None, "revision": 9},
            )

    def test_a_slot_naming_an_unverifiable_bundle_is_refused(self) -> None:
        first = self.create()
        (first / "brain.toml").write_text("tampered", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            bundle.write_state(
                self.root,
                {"candidate": first.name, "active": first.name,
                 "previous": None, "revision": 9},
            )


class LockingTests(_Fixture, unittest.TestCase):
    """Atomic replace stops a torn file, not a lost update."""

    def spy(self):
        events = []
        real_lock = bundle.exclusive_lock
        real_write = bundle.write_state

        import contextlib as _ctx

        @_ctx.contextmanager
        def watched_lock(root):
            events.append("lock")
            with real_lock(root):
                yield
            events.append("unlock")

        def watched_write(root, state):
            events.append("write")
            return real_write(root, state)

        return events, watched_lock, watched_write

    def assert_write_inside_lock(self, action) -> None:
        events, watched_lock, watched_write = self.spy()
        original_lock = bundle.exclusive_lock
        original_write = bundle.write_state
        bundle.exclusive_lock = watched_lock
        bundle.write_state = watched_write
        try:
            action()
        finally:
            bundle.exclusive_lock = original_lock
            bundle.write_state = original_write
        self.assertIn("lock", events)
        self.assertIn("write", events)
        self.assertLess(events.index("lock"), events.index("write"))
        self.assertGreater(events.index("unlock"), events.index("write"))

    def test_create_writes_under_the_lock(self) -> None:
        self.assert_write_inside_lock(self.create)

    def test_promote_writes_under_the_lock(self) -> None:
        self.create()
        self.assert_write_inside_lock(lambda: bundle.promote(self.root))

    def test_the_real_lock_takes_and_releases_an_exclusive_flock(self) -> None:
        """Prove the lock is a lock, not a context manager shaped like one."""
        calls = []
        original = bundle.fcntl.flock

        def watched(fileno, operation):
            calls.append(operation)
            return original(fileno, operation)

        bundle.fcntl.flock = watched
        try:
            with bundle.exclusive_lock(self.root):
                self.assertEqual(calls, [bundle.fcntl.LOCK_EX])
        finally:
            bundle.fcntl.flock = original

        self.assertEqual(calls, [bundle.fcntl.LOCK_EX, bundle.fcntl.LOCK_UN])

    def test_the_lock_is_released_even_when_the_body_raises(self) -> None:
        calls = []
        original = bundle.fcntl.flock

        def watched(fileno, operation):
            calls.append(operation)
            return original(fileno, operation)

        bundle.fcntl.flock = watched
        try:
            with self.assertRaises(RuntimeError), bundle.exclusive_lock(self.root):
                raise RuntimeError("boom")
        finally:
            bundle.fcntl.flock = original

        self.assertEqual(calls, [bundle.fcntl.LOCK_EX, bundle.fcntl.LOCK_UN])

    def test_record_rollback_writes_under_the_lock(self) -> None:
        self.create()
        bundle.promote(self.root)
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        self.create()
        bundle.promote(self.root)
        plan = bundle.plan_rollback(self.root)
        self.assert_write_inside_lock(lambda: bundle.record_rollback(self.root, plan))


class CommandLineTests(_Fixture, unittest.TestCase):
    """Exercise the real CLI, because a runbook prescribes commands, not calls.

    The parser once lacked --out and --plan while the runbook used both, so
    every documented rollback command exited 2 on unrecognized arguments and
    no unit test noticed.
    """

    SCRIPT = SCRIPTS / "brain_bundle.py"

    def run_cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args, "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def two_releases(self) -> tuple[str, str]:
        first = self.create()
        bundle.promote(self.root)
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        second = self.create()
        bundle.promote(self.root)
        return first.name, second.name

    def test_every_runbook_flag_is_accepted(self) -> None:
        first, second = self.two_releases()
        plan_file = self.base / "rollback-plan.json"

        planned = self.run_cli("plan-rollback", "--out", str(plan_file))

        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertIn(first, planned.stdout)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        self.assertEqual(plan["target"], first)
        self.assertEqual(plan["expected_active"], second)

        recorded = self.run_cli("record-rollback", "--plan", str(plan_file))

        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(bundle.read_state(self.root)["active"], first)

    def test_plan_rollback_without_out_is_refused(self) -> None:
        self.two_releases()

        result = self.run_cli("plan-rollback")

        self.assertEqual(result.returncode, 1)
        self.assertIn("--out is required", result.stderr)

    def test_record_rollback_without_plan_is_refused(self) -> None:
        self.two_releases()

        result = self.run_cli("record-rollback")

        self.assertEqual(result.returncode, 1)
        self.assertIn("--plan is required", result.stderr)

    def test_a_missing_plan_file_fails_cleanly(self) -> None:
        self.two_releases()

        result = self.run_cli("record-rollback", "--plan", str(self.base / "gone.json"))

        self.assertNotEqual(result.returncode, 0)

    def test_a_malformed_plan_file_fails_cleanly(self) -> None:
        self.two_releases()
        broken = self.base / "broken.json"
        broken.write_text("not json", encoding="utf-8")

        result = self.run_cli("record-rollback", "--plan", str(broken))

        self.assertNotEqual(result.returncode, 0)

    def test_a_plan_missing_a_field_is_refused(self) -> None:
        first, _ = self.two_releases()
        partial = self.base / "partial.json"
        partial.write_text(json.dumps({"target": first}), encoding="utf-8")

        result = self.run_cli("record-rollback", "--plan", str(partial))

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)

    def test_an_unknown_flag_is_still_rejected(self) -> None:
        result = self.run_cli("status", "--nonsense")

        self.assertEqual(result.returncode, 2)

    def test_status_runs(self) -> None:
        self.create()

        result = self.run_cli("status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("candidate", result.stdout)


class RunbookContractTests(unittest.TestCase):
    """The documented procedure must record state only after validating."""

    RUNBOOK = Path(__file__).resolve().parents[1] / "docs/runbook.md"

    @staticmethod
    def _joined(text: str) -> str:
        """Collapse shell line continuations so a command reads as one line."""
        return re.sub(r"\s*\\\n\s*", " ", text)

    def rollback_section(self) -> str:
        text = self.RUNBOOK.read_text(encoding="utf-8")
        start = text.index("### Rollback")
        return self._joined(text[start : text.index("### Architectural reversion")])

    def test_rollback_is_recorded_after_the_whole_validation_block(self) -> None:
        section = self.rollback_section()

        record = section.index("brain_bundle.py record-rollback")
        for gate in (
            "hermes_integration_check.py",
            "smoke_test.py",
            "hermes_integrity.py",
            "controlled CTWA",
        ):
            with self.subTest(gate=gate):
                self.assertLess(
                    section.index(gate),
                    record,
                    f"{gate} must be validated before the rollback is recorded",
                )

    def test_the_plan_is_captured_and_reused(self) -> None:
        """Re-planning after validation would defeat the compare-and-swap."""
        section = self.rollback_section()

        self.assertIn("plan-rollback --out", section)
        self.assertIn("record-rollback --plan", section)
        self.assertEqual(section.count("brain_bundle.py plan-rollback"), 1)

    def test_rollback_plans_before_it_installs(self) -> None:
        """Match the invocation, not the prose that describes it."""
        section = self.rollback_section()

        self.assertLess(
            section.index("brain_bundle.py plan-rollback"),
            section.index("systemctl restart"),
        )

    def test_the_window_promotes_only_after_validation(self) -> None:
        text = self.RUNBOOK.read_text(encoding="utf-8")
        window = text[text.index("### The window") : text.index("### Partial-deploy recovery")]

        promote = window.index("brain_bundle.py promote")
        for gate in (
            "hermes_integration_check.py",
            "smoke_test.py",
            "hermes_integrity.py",
            "controlled CTWA",
        ):
            with self.subTest(gate=gate):
                self.assertLess(window.index(gate), promote)


if __name__ == "__main__":
    unittest.main()
