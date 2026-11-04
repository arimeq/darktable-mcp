"""End-to-end checks against the real darktable-cli (5.6.1, the user's GUI version) on sample RAWs.

Run scripts/cloud-setup.sh first; it fetches darktable-cli and the samples into tests/data.
"""
import io
import re
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from darktable_mcp import server
from darktable_mcp.xmp_writer import xmp_sidecar_path

DATA = server.Path(__file__).parent / "data"


@pytest.fixture(params=["_MG_7191.CR2", "PA080192.ORF"])  # Canon EOS 450D, Olympus E-M10 Mark III
def raw(request, tmp_path):
    assert server.DARKTABLE_CLI, "darktable-cli not found: run scripts/cloud-setup.sh"
    assert (DATA / request.param).exists(), "sample RAW missing: run scripts/cloud-setup.sh"
    return str(shutil.copy(DATA / request.param, tmp_path))


def export_mean(raw: str, out: str) -> float:
    r = server.export_image(raw, output_directory=out, max_dimension=600)
    assert r["rendered_via"] == "darktable-cli", r
    return float(np.asarray(Image.open(r["output_path"]).convert("L")).mean())


def loaded_history(raw: str, tmp_path) -> list[tuple[str, bool]]:
    """(module, accepted as-is) for each history entry darktable read, from its `-d params` log."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "darktablerc").write_text("plugins/darkroom/workflow=scene-referred (sigmoid)\n")
    log = subprocess.run(
        [server.DARKTABLE_CLI, server.Path(raw).as_posix(), xmp_sidecar_path(server.Path(raw)).as_posix(),
         (tmp_path / "log.jpg").as_posix(), "--width", "200", "--height", "200", "--verbose",
         "--core", "-d", "params", "--configdir", cfg.as_posix(), "--library", ":memory:"],
        capture_output=True, text=True, timeout=600).stdout
    # develop.c dt_dev_read_history_ext: blendop then params, each "version ok|WRONG  params ok|WRONG"
    return [(m[0], "WRONG" not in m[1]) for m in
            re.findall(r"successfully loaded module (\w+) from history\n(.*\n.*)", log)]


def test_exposure_brightens_render(raw, tmp_path):
    base = export_mean(raw, str(tmp_path / "a"))
    server.apply_adjustments(raw, exposure_ev=1.0)
    assert export_mean(raw, str(tmp_path / "b")) > base * 1.2


def test_darktable_accepts_every_written_module(raw, tmp_path):
    server.apply_adjustments(raw, exposure_ev=0.5, contrast=0.3, black_level=0.01)
    server.export_image(raw, output_directory=str(tmp_path / "out"), max_dimension=200)
    ops = re.findall(r'darktable:operation="(\w+)"', xmp_sidecar_path(server.Path(raw)).read_text())
    assert ops
    # Our entries come after darktable's auto-applied ones, whose zero-length params always log WRONG
    assert loaded_history(raw, tmp_path)[-len(ops):] == [(op, True) for op in ops]


def export_gray(raw: str, out: str) -> np.ndarray:
    r = server.export_image(raw, output_directory=out, max_dimension=600)
    assert r["rendered_via"] == "darktable-cli", r
    return np.asarray(Image.open(r["output_path"]).convert("L"), float)


def test_darktable_accepts_masked_instances(raw, tmp_path):
    server.apply_adjustments(raw, exposure_ev=0.3, clarity=10, saturation=10)
    server.add_masked_adjustment(raw, {"exposure_ev": -1, "clarity": 30}, {
        "gradient": {"rotation": 0, "offset": 40, "compression": 10}, "luminance": [10, 20, 100, 100],
        "feathering_radius": 20, "blur_radius": 5, "contrast": 20, "brightness": -10, "opacity": 80})
    server.add_masked_adjustment(raw, {"saturation": 30, "shadows": 20}, {"hue": [330, 350, 20, 40]}, "warm & \"red\"")
    server.export_image(raw, output_directory=str(tmp_path / "out"), max_dimension=200)
    xmp = xmp_sidecar_path(server.Path(raw)).read_text()
    ops = re.findall(r'darktable:operation="(\w+)"', xmp)
    assert ops.count("bilat") == 2 and ops.count("colorbalancergb") == 2
    assert loaded_history(raw, tmp_path)[-len(ops):] == [(op, True) for op in ops]


def test_gradient_mask_confines_effect(raw, tmp_path):
    base = export_gray(raw, str(tmp_path / "a"))
    mid = server.add_masked_adjustment(raw, {"exposure_ev": 1.5}, {
        "gradient": {"rotation": 0, "offset": 50, "compression": 5}})["id"]
    top = export_gray(raw, str(tmp_path / "b"))
    server.update_masked_adjustment(raw, mid, mask={"invert": True})
    bottom = export_gray(raw, str(tmp_path / "c"))
    q = base.shape[0] // 4
    assert top[:q].mean() > base[:q].mean() + 15 and abs(top[-q:].mean() - base[-q:].mean()) < 2
    assert bottom[-q:].mean() > base[-q:].mean() + 15 and abs(bottom[:q].mean() - base[:q].mean()) < 2


def test_luminance_mask_selects_tones(raw, tmp_path):
    base = export_gray(raw, str(tmp_path / "a"))
    server.add_masked_adjustment(raw, {"exposure_ev": 1}, {"luminance": [0, 0, 5, 15]})
    lifted = export_gray(raw, str(tmp_path / "b")) - base
    dark, bright = base < np.percentile(base, 20), base > np.percentile(base, 80)
    assert lifted[dark].mean() > 3 * max(lifted[bright].mean(), 1)


def test_mask_preview_shows_gradient(raw):
    mid = server.add_masked_adjustment(raw, {"clarity": 20}, {"gradient": {"rotation": 90, "compression": 5}})["id"]
    server.add_masked_adjustment(raw, {"saturation": 20}, {"hue": [200, 220, 280, 300]})
    out = server.export_mask_preview(raw, mid, max_size=400)
    assert len(out) == 2 and "local contrast" in out[0], out
    m = np.asarray(Image.open(io.BytesIO(out[1].data)), float)
    w = m.shape[1] // 4
    assert m[:, :w].mean() > 200 and m[:, -w:].mean() < 50  # rotation 90 = left
