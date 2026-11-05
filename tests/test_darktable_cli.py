"""End-to-end checks against the real darktable-cli (5.6.1, the user's GUI version) on sample RAWs.

Run scripts/cloud-setup.sh first; it fetches darktable-cli and the samples into tests/data.
"""
import base64
import io
import json
import re
import shutil
import sqlite3
import struct
import zlib
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
    server.tone_mapper(raw, contrast=1.8, skew=-0.2, white_target=95, hue_preservation=80)
    server.color_equalizer(raw, saturation={"green": 20}, hue={"red": 5}, brightness={"blue": -20})
    server.local_contrast(raw, detail=140, highlights=30, shadows=70, midtone_range=0.4)
    server.rotate_image(raw, 2)
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


def set_orientation(raw: str, value: int) -> None:
    """Set the EXIF Orientation tag in IFD0 of a TIFF-based RAW (CR2, ORF) in place."""
    b = bytearray(server.Path(raw).read_bytes())
    ifd = struct.unpack_from("<I", b, 4)[0]
    e = next(ifd + 2 + 12 * i for i in range(struct.unpack_from("<H", b, ifd)[0])
             if struct.unpack_from("<H", b, ifd + 2 + 12 * i)[0] == 0x112)
    struct.pack_into("<H", b, e + 8, value)
    server.Path(raw).write_bytes(b)


@pytest.mark.parametrize("orientation", [1, 6, 8])  # landscape and both portrait turns, as the camera tags them
def test_gradient_rotation_is_upright(raw, orientation):
    set_orientation(raw, orientation)
    mid = server.add_masked_adjustment(raw, {"clarity": 20}, {"gradient": {"offset": 25, "compression": 2}})["id"]
    for rotation, side in ((0, "top"), (180, "bottom"), (-90, "right"), (90, "left")):
        server.update_masked_adjustment(raw, mid, mask={"gradient": {"rotation": rotation, "offset": 25, "compression": 2}})
        m = np.asarray(Image.open(io.BytesIO(server.export_mask_preview(raw, mid, max_size=400)[1].data)), float)
        assert (m.shape[0] > m.shape[1]) == (orientation != 1)
        m = {"top": m, "bottom": m[::-1], "left": m.T, "right": m.T[::-1]}[side]
        n = m.shape[0]
        # offset counts from the masked edge: full effect within the first 25 %, none past ~40 %
        assert m[:int(n * 0.15)].mean() > 200 and m[int(n * 0.4):].mean() < 20, (rotation, side)


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


def test_masks_saved_in_sensor_frame_keep_their_place(raw):
    set_orientation(raw, 6)  # portrait: darktable turns the sensor frame 90° clockwise
    mid = server.add_masked_adjustment(raw, {"clarity": 20}, {"gradient": {"offset": 25, "compression": 2}})["id"]
    sidecar = server.Path(raw).with_name(server.Path(raw).stem + ".mcp.json")
    old = json.loads(sidecar.read_text())
    del old["version"]
    old["masked"][0]["mask"]["gradient"]["rotation"] = 90  # sensor's left = top of the upright image
    sidecar.write_text(json.dumps(old))
    m = np.asarray(Image.open(io.BytesIO(server.export_mask_preview(raw, mid, max_size=400)[1].data)), float)
    assert m[:int(m.shape[0] * 0.15)].mean() > 200 and m[int(m.shape[0] * 0.4):].mean() < 20
    assert server.list_masked_adjustments(raw)["masked"][0]["mask"]["gradient"]["rotation"] == 0


def test_export_edges_have_no_black_blocks(raw, tmp_path):
    # the crop match_crop_to_jpeg set on P8192711, whose export had black blocks along the bottom on the user's PC
    server.apply_adjustments(raw, exposure_ev=-0.3, contrast=30, highlights=-50, shadows=20, clarity=15)
    server.add_masked_adjustment(raw, {"clarity": 20}, {"gradient": {"offset": 40, "compression": 3},
                                                        "feathering_radius": 30})
    server.crop_image(raw, left=0, top=0.001337, right=1, bottom=0.901070)
    a = np.asarray(Image.open(server.export_image(raw, output_directory=str(tmp_path))["output_path"]).convert("RGB"))
    black = a.max(axis=2) <= 2
    # dark scenes have stray black pixels, a block is a column of them through the outer 8 px
    assert {side: int(z[:8].all(axis=0).sum()) for side, z in
            dict(top=black, bottom=black[::-1], left=black.T, right=black.T[::-1]).items()} == dict.fromkeys(
        ("top", "bottom", "left", "right"), 0)


def test_tone_mapper_replaces_the_workflow_sigmoid(raw, tmp_path):
    base = export_gray(raw, str(tmp_path / "a"))
    server.tone_mapper(raw)  # darktable's defaults: must replace the auto-applied instance, not stack on it
    assert np.abs(export_gray(raw, str(tmp_path / "b")) - base).max() <= 1
    server.tone_mapper(raw, contrast=2.5)
    punchy = export_gray(raw, str(tmp_path / "c"))
    assert punchy.std() > base.std() * 1.1
    server.tone_mapper(raw, contrast=1.5, white_target=70)
    assert np.percentile(export_gray(raw, str(tmp_path / "d")), 99) < np.percentile(base, 99) - 5


def test_color_equalizer_targets_one_hue(raw, tmp_path):
    def hsv(name):
        r = server.export_image(raw, output_directory=str(tmp_path / name), max_dimension=400)
        return np.asarray(Image.open(r["output_path"]).convert("HSV"), float)
    base = hsv("a")
    h, s = base[..., 0] * 360 / 255, base[..., 1]
    warm, blue = (h > 15) & (h < 75) & (s > 60), (h > 190) & (h < 250) & (s > 60)
    region, own, other = ((blue, ("cyan", "blue"), ("orange", "yellow")) if blue.sum() > warm.sum()
                          else (warm, ("orange", "yellow"), ("cyan", "blue")))
    server.color_equalizer(raw, saturation=dict.fromkeys(other, -100))
    assert hsv("b")[..., 1][region].mean() > 0.9 * s[region].mean()
    server.color_equalizer(raw, reset=True)
    server.color_equalizer(raw, saturation=dict.fromkeys(own, -100))
    assert hsv("c")[..., 1][region].mean() < 0.6 * s[region].mean()
    server.color_equalizer(raw, reset=True)
    server.color_equalizer(raw, brightness=dict.fromkeys(own, -50))
    assert hsv("d")[..., 2][region].mean() < base[..., 2][region].mean() - 10


def test_local_contrast_detail_sets_texture(raw, tmp_path):
    def texture(name):
        g = export_gray(raw, str(tmp_path / name))
        return np.abs(np.diff(g, axis=1)).mean()  # fine structure: mean step between neighbours
    base = texture("a")
    server.local_contrast(raw)  # darktable's default, detail 125 %
    default = texture("b")
    server.local_contrast(raw, detail=200)
    strong = texture("c")
    server.local_contrast(raw, detail=60)
    assert texture("d") < base < default < strong


def test_renders_follow_edits_after_export(raw, tmp_path):
    # the user's sequence: export (writes the sidecar darktable-cli also imports), edit, re-render at the same size
    mid = server.add_masked_adjustment(raw, {"exposure_ev": 1}, {"gradient": {"compression": 3}})["id"]
    def renders():
        text, img = server.compare_images(raw, raw, max_size=300)
        preview = server.get_image_preview(raw, max_size=300)
        return (json.loads(text)["a"]["edit_hash"], json.loads(preview[0].split("\n", 1)[1])["edit_hash"],
                np.asarray(Image.open(io.BytesIO(img.data)).convert("L"), float).mean(),
                np.asarray(Image.open(io.BytesIO(preview[1].data)).convert("L"), float).mean(),
                server.get_histogram(raw)["luminance"])
    before = renders()
    server.export_image(raw, output_directory=str(tmp_path))
    server.update_masked_adjustment(raw, mid, {"exposure_ev": -2})
    after = renders()
    assert before[0] == before[1] != after[0] == after[1]
    assert after[2] < before[2] - 20 and after[3] < before[3] - 20 and after[4] != before[4]


def test_darktable_style_is_applied(raw, tmp_path, monkeypatch):
    # a user config whose data.db holds a style "mono": color balance rgb at chroma -100 %
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    cfg = tmp_path / "appdata" / "darktable"
    cfg.mkdir(parents=True)
    (cfg / "darktablerc").write_text("plugins/darkroom/workflow=scene-referred (sigmoid)\n")
    subprocess.run([server.DARKTABLE_CLI, raw, (tmp_path / "init.jpg").as_posix(), "--width", "50", "--height", "50",
                    "--core", "--configdir", cfg.as_posix(), "--library", ":memory:"], capture_output=True, timeout=600)
    blend = zlib.decompress(base64.b64decode(xmp_writer._BLENDOP_SCENE[4:]))
    with sqlite3.connect(cfg / "data.db") as con:
        sid = con.execute("INSERT INTO styles (name, description) VALUES ('mono', 'test')").lastrowid
        con.execute("INSERT INTO style_items VALUES (?, 0, 5, 'colorbalancergb', ?, 1, ?, 14, 0, 'mono', 1)",
                    (sid, xmp_writer._color_balance(-100, 0, 0), blend))
    assert server.darktable_style(raw)["styles"]["mono"] == "test"
    assert "error" in server.darktable_style(raw, "nope")
    def saturation(name):
        r = server.export_image(raw, output_directory=str(tmp_path / name), max_dimension=300)
        return np.asarray(Image.open(r["output_path"]).convert("HSV"), float)[..., 1].mean()
    base = saturation("a")
    server.darktable_style(raw, "mono")
    assert saturation("b") < base * 0.2
    server.darktable_style(raw, remove=True)
    assert saturation("c") == pytest.approx(base, abs=1)


def test_rotation_straightens_clockwise_without_black_corners(raw, tmp_path):
    base = Image.open(server.export_image(raw, output_directory=str(tmp_path / "a"), max_dimension=800)["output_path"])
    server.rotate_image(raw, 4)
    r = server.export_image(raw, output_directory=str(tmp_path / "b"), max_dimension=800)
    assert "stored_but_not_rendered" not in r and "ignored_by_darktable" not in r
    rot = Image.open(r["output_path"])
    assert rot.size == pytest.approx(base.size, abs=2)
    (W, H), c, s = base.size, np.cos(np.radians(4)), np.sin(np.radians(4))
    k = min(W / (W * c + H * s), H / (W * s + H * c))  # the crop that keeps the frame's proportions
    def match(degrees_cw):
        sim = base.convert("L").rotate(-degrees_cw, Image.BICUBIC).crop(
            ((W - k * W) / 2, (H - k * H) / 2, (W + k * W) / 2, (H + k * H) / 2)).resize(rot.size)
        return np.corrcoef(np.asarray(sim, float).ravel(), np.asarray(rot.convert("L"), float).ravel())[0, 1]
    assert match(4) > 0.95 > match(-4) + 0.2
    corners = np.asarray(rot.convert("L"), float)
    assert min(corners[:6, :6].mean(), corners[:6, -6:].mean(), corners[-6:, :6].mean(), corners[-6:, -6:].mean()) > 2
