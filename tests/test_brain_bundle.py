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


class BundleTests(unittest.TestCase):
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

    # ------------------------------------------------------------------

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


class BundleImmutabilityTests(BundleTests):
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


class SlotAtomicityTests(BundleTests):
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


class RollbackActionTests(BundleTests):
    def second_bundle(self) -> Path:
        (self.repo / "file.txt").write_text("two", encoding="utf-8")
        self.commit("second")
        return self.create()

    def test_rollback_makes_previous_active_and_records_the_swap(self) -> None:
        first = self.create()
        bundle.promote(self.root)
        second = self.second_bundle()
        bundle.promote(self.root)

        restored, displaced = bundle.rollback(self.root)

        self.assertEqual(restored, first.name)
        self.assertEqual(displaced, second.name)
        self.assertEqual((self.root / "active").resolve().name, first.name)
        self.assertEqual((self.root / "previous").resolve().name, second.name)

    def test_rollback_without_a_previous_bundle_is_refused(self) -> None:
        self.create()
        bundle.promote(self.root)

        with self.assertRaisesRegex(bundle.BundleError, "no previous"):
            bundle.rollback(self.root)

    def test_rollback_verifies_previous_before_swapping(self) -> None:
        first = self.create()
        bundle.promote(self.root)
        second = self.second_bundle()
        bundle.promote(self.root)
        (first / "brain.toml").write_text("tampered", encoding="utf-8")

        with self.assertRaises(bundle.BundleError):
            bundle.rollback(self.root)

        self.assertEqual((self.root / "active").resolve().name, second.name)


if __name__ == "__main__":
    unittest.main()
