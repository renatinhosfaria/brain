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

    def test_the_bundle_carries_plugin_and_config(self) -> None:
        created = self.create()

        self.assertTrue((created / "plugin" / "tools.py").is_file())
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


if __name__ == "__main__":
    unittest.main()
