import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import rpg_sdk_agent as sdk
from scripts.rpg_benchmark import output_paths, pins, run, suite


BASE = Path(__file__).resolve().parents[2]


class OutputRootTests(unittest.TestCase):
    def test_both_reservers_accept_explicit_external_roots_and_remain_exclusive(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            for reserve in (run.reserve_output, sdk.reserve_output):
                root = base / reserve.__module__.replace(".", "-") / "authorized"
                out = root / "nested" / "episode"
                with patch.object(pins, "ROOT", base / "default"), patch.object(
                        sdk, "OUTPUT_ROOT", base / "sdk-default"):
                    self.assertEqual(reserve(out, root), out)
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)
                sentinel = out / "retained"
                sentinel.write_text("unchanged")
                with self.assertRaises(FileExistsError):
                    reserve(out, root)
                self.assertEqual(sentinel.read_text(), "unchanged")
                for invalid in (root, base / "escape", root / ".." / "escape", Path("relative")):
                    with self.assertRaises(ValueError):
                        output_paths.validate_output(invalid, root)

    def test_root_validation_rejects_relative_traversal_files_and_filesystem_root(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            file = base / "file"
            file.write_text("retained")
            for root in (Path("relative"), base / ".." / "escape", Path("/"), file):
                with self.assertRaises(ValueError):
                    output_paths.reserve_output(base / "episode", root)
            self.assertFalse((base / "episode").exists())

    def test_symlink_root_ancestors_leaf_and_dangling_links_are_refused(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            dangling = root / "dangling"
            dangling.symlink_to(root / "absent", target_is_directory=True)
            for reserve in (run.reserve_output, sdk.reserve_output):
                for out, allowed in ((link / "episode", link), (link / "nested" / "episode", root),
                                     (dangling, root), (target / "new", link)):
                    with self.assertRaises(ValueError):
                        reserve(out, allowed)
            self.assertFalse((target / "new").exists())

    def test_runner_propagates_configured_sdk_root_and_default_follows_run_root(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            root = base / "researcher-runs"
            out = root / "episode"
            run_root, sdk_root, sdk_out = run.output_locations(out, root, check_sdk=True)
            self.assertEqual(run_root, root)
            self.assertEqual(sdk_root, root / "sdk-adapter")
            self.assertEqual(sdk_out, sdk_root / "episodes/episode")
            separate = base / "researcher-sdk"
            _, sdk_root, sdk_out = run.output_locations(out, root, separate, check_sdk=True)
            command = run.sdk_command("/python", "/adapter.py", base / "episode.sock",
                                      "macro goal", sdk_out, sdk_root)
            parsed = sdk.parse_args(command[4:])
            self.assertEqual(parsed.output_root, separate)
            self.assertEqual(parsed.out, separate / "episodes/episode")
            self.assertFalse(root.exists())
            self.assertFalse(separate.exists())

    def test_sdk_conflicts_reject_before_reserving_run_or_starting_native(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            root, sdk_root = base / "runs", base / "sdk"
            existing = sdk_root / "episodes/episode"
            existing.mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                run.output_locations(root / "episode", root, sdk_root, check_sdk=True)
            self.assertFalse(root.exists())
            fresh_sdk = base / "fresh-sdk"
            collision = fresh_sdk / "episodes/episode"
            for out in (collision, collision / "child/episode"):
                with self.assertRaises(ValueError):
                    run.output_locations(out, base, fresh_sdk, check_sdk=True)

    def test_sdk_invalid_root_fails_episode_preflight_without_allocating_or_native_launch(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            root = base / "runs"
            target = base / "target"
            target.mkdir()
            link = base / "sdk-link"
            link.symlink_to(target, target_is_directory=True)
            selected = {
                "output_root": str(root), "protected_paths": [],
                "sources": {"baseline": {"root": "/unused/source"}},
                "seeds": [130626853], "engine_root": "/unused/engine",
                "assets": {"root": "/unused/assets"}, "dependencies": {
                    "scene_manifest": {"path": str(Path(suite.__file__).with_name("scenes.json"))},
                    "server": {"path": "/unused/server"}, "bridge": {"path": "/unused/bridge"}},
            }
            args = run.parser().parse_args([
                "--pins", str(base / "unused.json"), "--pins-sha256", "unused",
                "--out", str(root / "episode"), "--output-root", str(root),
                "--sdk-output-root", str(link), "--mode", "capability",
                "--scene", "C1", "--terrain", "natural", "--harness", "copilot-sdk"])
            with patch.object(pins, "load", return_value=selected), patch.object(
                    run, "reserve_output") as reserve, patch.object(run, "private_server") as native:
                with self.assertRaises(ValueError):
                    asyncio.run(run.episode(args))
            reserve.assert_not_called()
            native.assert_not_called()

    def test_standalone_sdk_import_and_configurable_root_help_outside_snapshot(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            result = subprocess.run(
                [str(pins.PYTHON), str(Path(sdk.__file__)), "--help"],
                cwd=temp, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--output-root", result.stdout)


if __name__ == "__main__":
    unittest.main()
