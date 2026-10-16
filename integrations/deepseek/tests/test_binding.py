"""Portable no-world binding/contract tests; stdlib + installed core/adapter."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsh_external_v1 import adapter, binding
from scripts.rpg_benchmark import capabilities, external, lifecycle


class BindingTests(unittest.TestCase):
    def test_shim_exports_without_provider_imports(self):
        self.assertTrue(callable(adapter.build_launch))
        self.assertTrue(callable(adapter.collect_evidence))

    def test_only_absolute_local_paths(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            binding.absolute(Path("relative"))
        with self.assertRaisesRegex(ValueError, "invalid local"):
            binding.absolute(Path("/some/../traversal"))

    def test_runtime_requires_complete_npm_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "node_modules"):
                binding.runtime_inventory(Path(directory))
            (Path(directory) / "node_modules").mkdir()
            with self.assertRaisesRegex(ValueError, "Complete pinned"):
                binding.runtime_inventory(Path(directory))

    def test_runtime_hashes_internal_bin_link_and_detects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text("{}")
            (root / "package-lock.json").write_text("{}")
            (root / "node_modules/.bin").mkdir(parents=True)
            target = root / "node_modules/cli.js"
            target.write_text("// original")
            (root / "node_modules/.bin/dsh").symlink_to("../cli.js")
            before = binding.runtime_inventory(root)
            self.assertEqual(before["node_modules/.bin/dsh"], lifecycle.sha(target))
            target.write_text("// changed")
            self.assertNotEqual(before, binding.runtime_inventory(root))

    def test_runtime_rejects_external_and_directory_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "node_modules").mkdir()
            link = root / "node_modules/escape"
            link.symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "internal regular file"):
                binding.runtime_inventory(root)
            link.unlink()
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "internal regular file"):
                binding.runtime_inventory(root)

    def test_standalone_freeze_has_no_private_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "adapter.py").write_text("# original")
            freeze = root / "freeze.json"
            digest = binding.write(freeze, {
                "root": str(root), "files": {"adapter.py": lifecycle.sha(root / "adapter.py")}})
            external.verify_freeze(freeze, digest)
            (root / "adapter.py").write_text("# changed")
            with self.assertRaisesRegex(ValueError, "frozen input changed"):
                external.verify_freeze(freeze, digest)

    def test_exact_mode_mapping_preserved(self):
        manifest = {"schema": "rpg-external-adapter/3", "capabilities": binding.CAPABILITIES,
                    "default_configuration": binding.DEFAULT}
        for mode, profile in (("structured", "ordinary-renderer-native-v1"),
                              ("hybrid", "ordinary-renderer-native-v1"),
                              ("image", "visual-motor-v1")):
            config, declared, legacy = capabilities.negotiate(
                manifest, {"observation_mode": mode, "action_profile": profile})
            self.assertFalse(legacy)
            capabilities.admit_backend(config, declared, "renderer")
        with self.assertRaisesRegex(ValueError, "mapping"):
            capabilities.negotiate(manifest, {"observation_mode": "image"})
        with self.assertRaisesRegex(ValueError, "model/reasoning"):
            capabilities.negotiate(manifest, {"model": "unclaimed-provider"})

    def test_binding_rebases_paths_not_content_and_keeps_protected_processes(self):
        base = {"schema": "rpg-runtime/1", "renderer": {"executable": "native/client"},
                "external_adapters": {}, "sources": {},
                "protected_processes": [{"pid": 1, "start_ticks": 2, "port": 3}]}
        result = binding.rebase(base, Path("/absolute/base"))
        self.assertEqual(result["renderer"]["executable"], "/absolute/base/native/client")
        self.assertEqual(result["protected_processes"], base["protected_processes"])
        self.assertEqual(base["renderer"]["executable"], "native/client")

    def test_launch_config_rejects_malformed_and_different_runtime_before_entry(self):
        context = {"engineering": False, "config": {"runtime": "relative"}}
        with self.assertRaisesRegex(ValueError, "installed official"):
            adapter.validate_config(context)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "node_modules/.bin").mkdir(parents=True)
            (root / "node_modules/.bin/dsh").touch()
            context["config"]["runtime"] = str(root)
            context["runtime"] = {"preflight": ["--runtime", "/another/runtime"]}
            with self.assertRaisesRegex(ValueError, "preflight-bound"):
                adapter.validate_config(context)
        context["config"] = {"runtime": "/unused", "unexpected": "option"}
        with self.assertRaisesRegex(ValueError, "Unknown"):
            adapter.validate_config(context)

    def test_invalid_input_does_not_allocate_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "not-created"
            with patch.object(binding.runtime, "load_runtime",
                              side_effect=ValueError("invalid input before any world")):
                with self.assertRaisesRegex(ValueError, "before any world"):
                    binding.bind(config=root / "config.json", config_sha256="0" * 64,
                                 python=Path("/usr/bin/python3"), selected_runtime=root,
                                 out=output, output_root=root / "runs")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
