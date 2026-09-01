from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

spec = importlib.util.spec_from_file_location(
    "hermes_update_guard", SCRIPTS / "hermes_update_guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def snap(*, upstream="aaa", home="bbb", files=None, dirty_home="", dirty_up="",
         health=None) -> dict:
    return {
        "upstream": {"head": upstream, "dirty": dirty_up},
        "hermes_home": {"head": home, "dirty": dirty_home},
        "files": files if files is not None else {"/root/.hermes/SOUL.md": "d1"},
        "health": health or {"brain": "ok", "observer": "ok"},
    }


class CompareTests(unittest.TestCase):
    def test_an_update_that_only_moves_upstream_is_clean(self) -> None:
        expected, findings = guard.compare(snap(), snap(upstream="ccc"))

        self.assertEqual(findings, [])
        self.assertTrue(any("upstream moved" in line for line in expected))

    def test_the_moved_upstream_is_reported_as_invalidating_the_baseline(self) -> None:
        """An operator who is not told this leaves a permanently red gate."""
        expected, _ = guard.compare(snap(), snap(upstream="ccc"))

        self.assertTrue(any("integrity baseline" in line for line in expected))

    def test_an_upstream_that_did_not_move_is_suspicious(self) -> None:
        _, findings = guard.compare(snap(), snap())

        self.assertTrue(any("did the update actually run" in f for f in findings))

    def test_a_changed_fama_file_is_a_finding(self) -> None:
        _, findings = guard.compare(
            snap(), snap(upstream="ccc", files={"/root/.hermes/SOUL.md": "d2"})
        )

        self.assertTrue(any("CHANGED" in f for f in findings))

    def test_a_removed_fama_file_is_a_finding(self) -> None:
        _, findings = guard.compare(
            snap(), snap(upstream="ccc", files={"/root/.hermes/SOUL.md": None})
        )

        self.assertTrue(any("REMOVED" in f for f in findings))

    def test_a_dirtied_operational_repository_is_a_finding(self) -> None:
        _, findings = guard.compare(
            snap(), snap(upstream="ccc", dirty_home=" M profiles/reno/config.yaml")
        )

        self.assertTrue(any("now dirty" in f for f in findings))

    def test_a_commit_in_the_operational_repository_is_a_finding(self) -> None:
        _, findings = guard.compare(snap(), snap(upstream="ccc", home="zzz"))

        self.assertTrue(any("should not" in f for f in findings))

    def test_a_dirty_upstream_after_the_update_is_a_finding(self) -> None:
        _, findings = guard.compare(snap(), snap(upstream="ccc", dirty_up=" M x.py"))

        self.assertTrue(any("dirty after the update" in f for f in findings))

    def test_an_unreachable_service_is_a_finding(self) -> None:
        _, findings = guard.compare(
            snap(),
            snap(upstream="ccc", health={"brain": "<unreachable>", "observer": "ok"}),
        )

        self.assertTrue(any("unreachable" in f for f in findings))


class WatchListTests(unittest.TestCase):
    def test_the_reno_allowlist_and_config_are_watched(self) -> None:
        """Config migration is the one risk this audit could not prove benign."""
        watched = {str(p) for p in guard.WATCHED}

        self.assertIn("/root/.hermes/profiles/reno/config.yaml", watched)
        self.assertIn("/root/.hermes/ops/hermes-team/reno-famachat-allowlist.json", watched)

    def test_every_plugin_source_file_is_watched(self) -> None:
        watched = {str(p) for p in guard.WATCHED}

        for name in ("__init__.py", "tools.py", "schemas.py", "plugin.yaml"):
            with self.subTest(name=name):
                self.assertIn(f"/root/.hermes/plugins/brain-ceo-bridge/{name}", watched)

    def test_brain_config_is_watched(self) -> None:
        self.assertIn(Path("/etc/brain/brain.toml"), guard.WATCHED)


if __name__ == "__main__":
    unittest.main()
