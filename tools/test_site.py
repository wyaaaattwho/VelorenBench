"""Focused, no-dependency publication checks; never launch native worlds/models."""
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def payload():
    js = (DOCS / "tasks_data.js").read_text()
    return json.loads(js.split("window.VELOREN_DATA = ", 1)[1].removesuffix(";\n"))


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.references, self.labels = [], [], []

    def handle_starttag(self, tag, attrs):
        row = dict(attrs)
        if "id" in row:
            self.ids.append(row["id"])
        for name in ("src", "poster"):
            if name in row:
                self.references.append(row[name])
        if tag == "link" and row.get("rel") in ("stylesheet", "icon"):
            self.references.append(row["href"])
        if tag == "label" and "for" in row:
            self.labels.append(row["for"])


class SiteContract(unittest.TestCase):
    def test_generated_content_is_current(self):
        for path, expected in module("build_site").generate().items():
            self.assertEqual(path.read_text(), expected, str(path.relative_to(ROOT)))

    def test_catalog_goals_terrains_requirements_and_modes_are_not_redefined(self):
        data = payload()
        scenes = json.loads((ROOT / "harness/scripts/rpg_benchmark/scenes.json").read_text())["scenes"]
        self.assertEqual(len(data["tasks"]), 29)
        self.assertEqual(len(data["families"]), 8)
        self.assertEqual(sum(len(s["terrains"]) for s in scenes), 37)
        for scene in scenes:
            task = next(t for t in data["tasks"] if t["id"] == scene["id"])
            self.assertEqual(task["terrain"], scene["terrains"])
            self.assertEqual(task["family"], scene["kind"])
            self.assertEqual(task["modalities"], ["structured", "hybrid", "image"])
            if "task" in scene:
                self.assertEqual(task["publicGoal"], scene["task"])
                self.assertEqual(task["goalKind"], "authored")
            else:
                self.assertEqual(task["goalKind"], "fixture-generated-catalog-description")
            for lang in ("en", "zh"):
                for key in ("title", "initial", "challenge", "oracle"):
                    self.assertGreater(len(task[lang][key]), 4)
        t05 = next(t for t in data["tasks"] if t["id"] == "T05")
        self.assertEqual(t05["imagePolicy"], "task-engineering-only")

    def test_media_is_complete_native_size_hashed_and_not_task_relabelled(self):
        data = payload()
        media = json.loads((DOCS / "assets/media-provenance.json").read_text())
        self.assertEqual({t["id"] for t in data["tasks"]}, set(media["screenshots"]))
        self.assertEqual(len(list((DOCS / "assets/tasks").glob("*.png"))), 29)
        for task in data["tasks"]:
            row = media["screenshots"][task["id"]]
            image = (DOCS / row["published_file"]).read_bytes()
            self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(image[16:20], "big"), 960)
            self.assertEqual(int.from_bytes(image[20:24], "big"), 540)
            self.assertEqual(hashlib.sha256(image).hexdigest(), row["published_sha256"])
            self.assertEqual(row["task_id"], task["id"])
            self.assertIn(row["terrain"], task["terrain"])
            self.assertEqual(task["image"], row["published_file"])
            self.assertIn("unchanged", row["transformation"])
        self.assertEqual(len(media["videos"]), 4)
        for video in media["videos"]:
            clip = DOCS / video["published_file"]
            self.assertEqual(hashlib.sha256(clip.read_bytes()).hexdigest(), video["published_sha256"])
            self.assertFalse(video["contains_terminal_hold"])
            self.assertEqual(video["duration_seconds"], 14)
            fallback = DOCS / video["fallback_file"]
            self.assertEqual(hashlib.sha256(fallback.read_bytes()).hexdigest(), video["fallback_sha256"])
            self.assertEqual(fallback.suffix, ".webm")

    def test_validation_preserves_retries_holds_and_denominators(self):
        data = payload()["validation"]
        self.assertEqual(len(data["cells"]), 36)
        self.assertEqual(data["cellCount"], 36)
        self.assertEqual(data["fullLiveVideos"], 27)
        self.assertEqual(data["terminalHoldVideos"], 9)
        self.assertEqual(data["retryCount"], 6)
        self.assertEqual(data["runtimeModelMetadata"], "unknown")
        expected = {(t, h, m) for t in data["sampledTasks"] for h in ("gameagent", "dsh")
                    for m in ("structured", "hybrid", "image")}
        self.assertEqual(expected, {(c["task"], c["harness"], c["modality"]) for c in data["cells"]})
        retries = [c for c in data["cells"] if c["sourceCell"] != c["id"]]
        self.assertEqual(len(retries), 6)
        for row in retries:
            self.assertNotEqual(row["originalResultSha256"], row["effectiveResultSha256"])
        self.assertEqual(sum(c["terminalHoldSeconds"] > 0 for c in data["cells"]), 9)
        self.assertAlmostEqual(sum(c["videoSeconds"] for c in data["cells"]), data["videoSeconds"])

    def test_markup_local_assets_and_accessible_hooks(self):
        parser = Tags()
        parser.feed((DOCS / "index.html").read_text())
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for ref in parser.references:
            self.assertTrue((DOCS / ref).is_file(), ref)
        for element in ("language", "quest-dialog", "quest-dialog-title", "task-count",
                        "modality-panel", "validation-body", "copy-status"):
            if element != "quest-dialog-title":  # inserted with the selected quest
                self.assertIn(element, parser.ids)
        css = (DOCS / "style.css").read_text()
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("@import", css)
        for url in re.findall(r'url\("([^"]+)"\)', css):
            self.assertTrue((DOCS / url).is_file(), url)

    def test_site_is_inside_the_source_inventory(self):
        files = module("package_source").source_inventory(ROOT)
        for path in DOCS.rglob("*"):
            if path.is_file():
                self.assertIn(path.relative_to(ROOT).as_posix(), files)


if __name__ == "__main__":
    unittest.main(verbosity=2)
