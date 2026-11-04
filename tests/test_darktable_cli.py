"""End-to-end checks against the real darktable-cli (5.6.1, the user's GUI version) on sample RAWs.

Run scripts/cloud-setup.sh first; it fetches darktable-cli and the samples into tests/data.
"""
import io
import re
import shutil
import struct
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageEnhance

from darktable_mcp import server, xmp_writer
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


def test_gradient_leaves_far_side_untouched(raw, tmp_path):
    base = export_gray(raw, str(tmp_path / "a"))
    h = base.shape[0]
    # compression 3 % of the diagonal: the mask is exactly 0 from 4 × 3 % of it (~22 % of a 3:2 height) past the line
    gradient = {"gradient": {"rotation": 0, "offset": 40, "compression": 3}}
    mid = server.add_masked_adjustment(raw, {"exposure_ev": 1}, gradient)["id"]
    diff = np.abs(export_gray(raw, str(tmp_path / "b")) - base)
    assert diff[:int(h * 0.4)].mean() > 5 and diff[int(h * 0.68):].max() == 0
    # local contrast works in Lab: blending in RGB (scene) round-trips Lab → RGB → Lab, ±1 in a few pixels
    server.update_masked_adjustment(raw, mid, {"exposure_ev": 0, "clarity": 60})
    diff = np.abs(export_gray(raw, str(tmp_path / "c")) - base)
    assert diff[:int(h * 0.4)].mean() > 1 and diff[int(h * 0.68):].max() <= 1
    assert (diff[int(h * 0.68):] > 0).mean() < 1e-3


@pytest.mark.parametrize("orientation", [5, 6])  # the two portrait turns, forced with darktable's flip module
def test_gradient_rotation_follows_portrait_orientation(raw, orientation, monkeypatch):
    def flipped(state, xmp_text=server.xmp_text):
        x, n = xmp_text(state), int(re.search(r'history_end="(\d+)"', xmp_text(state))[1])
        flip = xmp_writer._ITEM.format(num=n, op="flip", ver=2, params=struct.pack("<i", orientation).hex(), name="",
                                       blendop=xmp_writer._BLENDOP, prio=0, edited=0)
        end = "    </rdf:Seq>\n   </darktable:history>"
        return x.replace(f'history_end="{n}"', f'history_end="{n + 1}"').replace(end, flip + end)
    monkeypatch.setattr(server, "xmp_text", flipped)
    monkeypatch.setattr(xmp_writer, "raw_flip", lambda src: orientation)
    mid = server.add_masked_adjustment(raw, {"clarity": 20}, {"gradient": {"rotation": 0, "compression": 3}})["id"]
    m = np.asarray(Image.open(io.BytesIO(server.export_mask_preview(raw, mid, max_size=400)[1].data)), float)
    q = m.shape[0] // 4
    assert m.shape[0] > m.shape[1] and m[:q].mean() > 200 and m[-q:].mean() < 50


def test_region_preview_renders_at_full_resolution(raw):
    server.crop_image(raw, left=0.1, top=0.1, right=0.9, bottom=0.9)
    text, img = server.get_image_preview(raw, max_size=3000, region=[0.4, 0.4, 0.1, 0.1])
    full_w, full_h = server._full_size(server.Path(raw))
    assert '"scale": 1.0' in text
    assert Image.open(io.BytesIO(img.data)).size == pytest.approx((full_w * 0.08, full_h * 0.08), abs=3)


def test_match_crop_to_camera_jpeg(raw, tmp_path):
    full = Image.open(server.export_image(raw, output_directory=str(tmp_path / "full"), quality=95)["output_path"])
    w, h = full.size
    # a "camera JPEG": cropped, with a punchier look than darktable's
    cam = ImageEnhance.Contrast(ImageEnhance.Color(full.crop((int(.2 * w), int(.15 * h), int(.85 * w), int(.7 * h))))
                                .enhance(1.5)).enhance(1.3)
    cam.save(tmp_path / "cam.jpg", quality=90)
    r = server.match_crop_to_jpeg(raw, str(tmp_path / "cam.jpg"))
    assert r["score"] > 0.8, r
    assert [r["crop"][k] for k in ("left", "top", "right", "bottom")] == pytest.approx([.2, .15, .85, .7], abs=0.003)
    text, img = server.compare_images(raw, str(tmp_path / "cam.jpg"), max_size=400)
    side = Image.open(io.BytesIO(img.data))
    assert side.width == pytest.approx(2 * side.height * cam.width / cam.height + 8, abs=4)


def test_export_overwrite_replaces_named_output(raw, tmp_path):
    first = server.export_image(raw, output_directory=str(tmp_path), max_dimension=200)["output_path"]
    assert server.export_image(raw, output_directory=str(tmp_path), max_dimension=200)["output_path"] != first
    server.apply_adjustments(raw, exposure_ev=1)
    r = server.export_image(raw, output_directory=str(tmp_path), max_dimension=200, overwrite=True)
    assert r["output_path"] == first and r["rendered_via"] == "darktable-cli"
