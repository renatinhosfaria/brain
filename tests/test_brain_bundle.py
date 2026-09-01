from __future__ import annotations

import importlib.util
import json
import subprocess
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
    """Planning a rollback must not record one."""

    def two_releases(self) -> tuple[Path, Path]:
        first = self.create()
        bundle.promote(self.root)
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        second = self.create()
        bundle.promote(self.root)
        return first, second

    def test_planning_verifies_previous_and_changes_nothing(self) -> None:
        first, second = self.two_releases()
        before = bundle.read_state(self.root)

        target, outgoing = bundle.plan_rollback(self.root)

        self.assertEqual(target, first.name)
        self.assertEqual(outgoing, second.name)
        self.assertEqual(bundle.read_state(self.root), before)

    def test_planning_refuses_an_unverifiable_previous(self) -> None:
        first, _ = self.two_releases()
        (first / "brain.toml").write_text("tampered", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            bundle.plan_rollback(self.root)

    def test_a_failed_validation_leaves_the_last_validated_deployment(self) -> None:
        """Planning then abandoning must not move the authoritative state."""
        _, second = self.two_releases()

        bundle.plan_rollback(self.root)
        # Validation fails here, so record_rollback is never reached.

        self.assertEqual(bundle.read_state(self.root)["active"], second.name)

    def test_recording_swaps_active_and_previous(self) -> None:
        first, second = self.two_releases()

        bundle.plan_rollback(self.root)
        restored, displaced = bundle.record_rollback(self.root)

        self.assertEqual((restored, displaced), (first.name, second.name))
        state = bundle.read_state(self.root)
        self.assertEqual(state["active"], first.name)
        self.assertEqual(state["previous"], second.name)

    def test_recording_without_a_previous_bundle_is_refused(self) -> None:
        self.create()
        bundle.promote(self.root)

        with self.assertRaisesRegex(bundle.BundleError, "no previous"):
            bundle.record_rollback(self.root)


class RunbookContractTests(unittest.TestCase):
    """The documented procedure must record state only after validating."""

    RUNBOOK = Path(__file__).resolve().parents[1] / "docs/runbook.md"

    def rollback_section(self) -> str:
        text = self.RUNBOOK.read_text(encoding="utf-8")
        start = text.index("### Rollback")
        return text[start : text.index("### Architectural reversion")]

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
