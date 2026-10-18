"""Public asset preparation uses synthetic configs, never bundled game assets."""
import hashlib
import json
import subprocess
import sys

import pytest

from .test_public_source import tool_module, tools_path


@pytest.fixture
def stock(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tools_path()))
    tool = tool_module("prepare_assets")
    assets = tmp_path / "stock/assets"
    expected = {}
    # Independent synthetic expected bytes: no upstream config/art is distributed.
    for name, (base, creature, body, species) in tool.OVERLAYS.items():
        header = b"// synthetic test entity\nEntityConfig(\n"
        suffix = b"\n    loadout: FromBody,\n)\n"
        original = header + f'    body: RandomWith("{creature}"),'.encode() + suffix
        fixed = (header + f"    body: Exact({body}((\n".encode()
                 + f"        species: {species},\n".encode()
                 + b"        body_type: Male,\n    )))," + suffix)
        path = assets / base
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
        expected[name] = fixed
    (assets / "credits.ron").write_bytes(b"synthetic upstream terms, not an MIT relicense\n")
    files = tool.inventory(assets)
    files.update({name: hashlib.sha256(contents).hexdigest()
                  for name, contents in expected.items()})
    manifest = tmp_path / "assets-manifest.json"
    manifest.write_text(json.dumps({
        "files_sha256": files, "tree_sha256": tool.tree_digest(files)}))
    return tool, assets, manifest, expected


def test_dry_run_then_materialize_exact_assets_without_changing_stock(stock, tmp_path):
    tool, assets, manifest, overlays = stock
    before = tool.inventory(assets)
    out = tmp_path / "prepared"
    check = tool.prepare(assets, out, manifest=manifest, check=True)
    assert check["status"] == "checked"
    assert not out.exists()
    receipt = tool.prepare(assets, out, manifest=manifest)
    assert receipt["status"] == "prepared"
    assert json.loads((out / "receipt.json").read_text()) == receipt
    assert tool.inventory(out / "assets") == json.loads(manifest.read_text())["files_sha256"]
    assert (out / "assets/credits.ron").read_bytes() == (assets / "credits.ron").read_bytes()
    for name, contents in overlays.items():
        assert (out / "assets" / name).read_bytes() == contents
        assert not (assets / name).exists()
    assert tool.inventory(assets) == before
    # A previously complete, exact inventory is also valid input, never mutated.
    complete = tool.inventory(out / "assets")
    tool.prepare(out / "assets", tmp_path / "second", manifest=manifest)
    assert tool.inventory(out / "assets") == complete
    with pytest.raises(ValueError, match="output must not exist"):
        tool.prepare(assets, out, manifest=manifest)


@pytest.mark.parametrize("mutation", ["stock", "extra", "missing", "overlay", "symlink"])
def test_asset_preparation_rejects_non_pinned_input_without_output(stock, tmp_path, mutation):
    tool, assets, manifest, overlays = stock
    name, (base, *_) = next(iter(tool.OVERLAYS.items()))
    if mutation == "stock":
        (assets / base).write_bytes(b"wrong stock")
    elif mutation == "extra":
        (assets / "unlisted.ron").write_bytes(b"extra")
    elif mutation == "missing":
        (assets / base).unlink()
    elif mutation == "overlay":
        path = assets / name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"wrong existing derivative")
    else:
        (assets / base).unlink()
        (assets / base).symlink_to(assets / "credits.ron")
    out = tmp_path / "rejected"
    with pytest.raises(ValueError, match="inventory mismatch|symlink"):
        tool.prepare(assets, out, manifest=manifest)
    assert not out.exists()


@pytest.mark.parametrize("mutation", ["output_hash", "tree", "traversal"])
def test_generated_hash_and_manifest_cannot_be_silently_relaxed(stock, tmp_path, mutation):
    tool, assets, manifest, overlays = stock
    identity = json.loads(manifest.read_text())
    if mutation == "output_hash":
        identity["files_sha256"][next(iter(overlays))] = "0" * 64
        identity["tree_sha256"] = tool.tree_digest(identity["files_sha256"])
    elif mutation == "tree":
        identity["tree_sha256"] = "0" * 64
    else:
        identity["files_sha256"]["../outside"] = "0" * 64
        identity["tree_sha256"] = tool.tree_digest(identity["files_sha256"])
    manifest.write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="SHA-256 mismatch|manifest|inventory entry"):
        tool.prepare(assets, tmp_path / "rejected", manifest=manifest)
    assert not (tmp_path / "rejected").exists()


def test_outputs_must_be_new_disjoint_and_symlink_free(stock, tmp_path):
    tool, assets, manifest, overlays = stock
    for out in (assets, assets / "nested", assets.parent, tool.ROOT / "test-assets"):
        with pytest.raises(ValueError, match="overlaps"):
            tool.prepare(assets, out, manifest=manifest)
    link = tmp_path / "linked-stock"
    link.symlink_to(assets.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        tool.prepare(link / "assets", tmp_path / "rejected", manifest=manifest)


def test_overlay_preserves_loadout_and_normalizes_only_pinned_format(stock):
    tool, assets, manifest, overlays = stock
    source = (b'    body: RandomWith("green_legoom"),\n'
              b'    inherit: Asset("synthetic.loadout"),\n        )),    ),')
    result = tool.entity_overlay(source, "green_legoom", "BipedSmall", "GreenLegoom")
    assert b'inherit: Asset("synthetic.loadout")' in result
    assert result.endswith(b"        )),\n    ),\n")
    with pytest.raises(ValueError, match="one stock random-body"):
        tool.entity_overlay(source + source, "green_legoom", "BipedSmall", "GreenLegoom")


def test_public_asset_cli_exposes_read_only_check_from_any_cwd(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", str(tools_path() / "prepare_assets.py"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "--assets" in result.stdout and "--out" in result.stdout and "--check" in result.stdout
