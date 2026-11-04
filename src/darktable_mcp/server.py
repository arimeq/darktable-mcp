"""Darktable MCP Server — photo editing tools for Claude."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import Image, MCPServer

from .edits import HUES, ColorEqualizerState, CropState, EditState, GraduatedState, SigmoidState, MaskedAdjustment, MaskState
from .processor import ALL_EXTENSIONS, RAW_EXTENSIONS, ImageProcessor
from .xmp_writer import (MASKABLE, TURN, foreign_edits, foreign_sidecar, ignored_by_darktable, raw_flip,
                         xmp_sidecar_path, xmp_text)

mcp = MCPServer("DarktableMCP")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_dt_cli_windows() -> Optional[str]:
    candidate = r"C:\Program Files\darktable\bin\darktable-cli.exe"
    return candidate if Path(candidate).exists() else None


DARKTABLE_CLI = shutil.which("darktable-cli") or _find_dt_cli_windows()


def _load_or_new(image_path: str) -> tuple[Path, EditState]:
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    state = EditState.load(path) or EditState(source_path=path)
    if state.version < 2:
        # Older gradients were stored in the sensor's frame: turning them by the same angle keeps them in place
        if gradients := [m.mask.gradient for m in state.masked if m.mask.gradient]:
            turn = TURN[raw_flip(path)]
            for g in gradients:
                g.rotation -= turn
        state.version = 2
    return path, state


def _darktable_notes(path: Path, state: EditState) -> dict:
    """Why a darktable-cli render may not show the stored edits; empty if it shows all of them."""
    if path.suffix.lower() not in RAW_EXTENSIONS or not DARKTABLE_CLI:
        return {}
    name = xmp_sidecar_path(path).name
    if foreign_edits(path):
        return {"foreign_sidecar": str(xmp_sidecar_path(path)),
                "warning": f"{name} holds the user's own darktable edits. Preview and export render it as-is: "
                           "NONE of the edits made with this server's tools are applied, and the sidecar is "
                           "never overwritten."}
    notes = {}
    if foreign_sidecar(path):
        notes["sidecar_note"] = (f"{name} was written by darktable or another tool but holds no darktable edits. "
                                 "Your edits are rendered, but never saved into it.")
    if ignored := ignored_by_darktable(state):
        notes |= {"ignored_by_darktable": ignored,
                  "warning": "These edits are stored but have no effect on the preview or export; "
                             "changing them further does nothing."}
    return notes


def _render_xmp(path: Path, state: EditState, tmp: str, persist: bool) -> Path:
    """The XMP darktable-cli renders with: a foreign sidecar with edits as-is, else our edits, written as the
    real sidecar only when *persist* and it isn't foreign (previews must not touch it: darktable may be running)."""
    if foreign_edits(path):
        return xmp_sidecar_path(path)
    xmp = xmp_sidecar_path(path) if persist and not foreign_sidecar(path) else Path(tmp, "edit.xmp")
    xmp.write_text(xmp_text(state), encoding="utf-8")
    return xmp


def _render(path: Path, state: EditState, max_size: int) -> tuple:
    """Render with the pipeline export uses, so what the model sees is what gets exported. Returns (image, info)."""
    if path.suffix.lower() not in RAW_EXTENSIONS:
        return ImageProcessor(path).process(state, preview_size=max_size), {"rendered_via": "pillow"}
    if not DARKTABLE_CLI:
        return ImageProcessor(path).process(state, preview_size=max_size), {
            "rendered_via": "rawpy", "darktable_cli_failure": {"reason": "darktable-cli not found"},
            "warning": "Preview and export both use rawpy, but the XMP sidecar will look different in darktable."}
    from PIL import Image as PILImage
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp, "preview.jpg")
        result = _export_via_darktable_cli(path, dst, _render_xmp(path, state, tmp, False), 90, max_size)
        if result["status"] == "ok":
            with PILImage.open(dst) as img:
                return img.convert("RGB"), {"rendered_via": "darktable-cli"} | _darktable_notes(path, state)
    return ImageProcessor(path).process(state, preview_size=max_size), {
        "rendered_via": "rawpy", "darktable_cli_failure": {k: v for k, v in result.items() if k != "status"},
        "warning": "darktable-cli could not render this RAW, so this is an approximation with rawpy: "
                   "it may not match the exported image."}


def _quick_preview(path: Path, state: EditState, max_size: int) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    img, info = _render(path, state, max_size)
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), info


def _full_size(path: Path) -> tuple[int, int]:
    """Uncropped size of the upright image in px (for RAWs LibRaw's visible area, within a few px of darktable's)."""
    if path.suffix.lower() not in RAW_EXTENSIONS:
        from PIL import Image as PILImage, ImageOps
        with PILImage.open(path) as img:
            return ImageOps.exif_transpose(img).size
    import rawpy
    with rawpy.imread(str(path)) as raw:
        w, h = raw.sizes.width, raw.sizes.height
        return (h, w) if raw.sizes.flip & 4 else (w, h)


def _region_state(state: EditState, region: Optional[list[float]]) -> EditState:
    """A copy of *state* cropped further to *region* = [x, y, w, h], normalised to the current crop.

    The crop module then hands darktable only that part, so it renders at full resolution without the rest.
    """
    if not region:
        return state
    x, y, w, h = region
    c = state.crop or CropState()
    cw, ch = c.right - c.left, c.bottom - c.top
    return replace(state, crop=replace(c, left=c.left + x * cw, top=c.top + y * ch,
                                       right=c.left + (x + w) * cw, bottom=c.top + (y + h) * ch))


def _save_preview(path: Path, img_bytes: bytes, open_file: bool) -> str:
    import os
    preview_path = path.with_name(path.stem + "__preview.jpg")
    preview_path.write_bytes(img_bytes)
    if open_file:
        try:
            os.startfile(str(preview_path))   # Windows: opens in default image viewer
        except Exception:
            pass
    return str(preview_path)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_images(directory: str) -> list[dict]:
    """List all supported image files (RAW and raster) in *directory*.

    Returns a list of dicts with keys: path, name, type, has_edits, output_name.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return [{"error": f"Directory not found: {directory}"}]

    results = []
    for file in sorted(dir_path.iterdir()):
        if file.suffix.lower() not in ALL_EXTENSIONS:
            continue
        state = EditState.load(file)
        results.append({
            "path": str(file),
            "name": file.name,
            "type": "raw" if file.suffix.lower() in RAW_EXTENSIONS else "image",
            "has_edits": state.has_changes() if state else False,
            "output_name": state.output_name if state else None,
        })
    return results


@mcp.tool()
def get_image_info(image_path: str) -> dict:
    """Return detailed metadata (EXIF, dimensions, camera info) and current edit state for an image."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    proc = ImageProcessor(path)
    info = proc.get_info()
    info["edits"] = state.to_dict()
    info["darktable_cli_available"] = DARKTABLE_CLI is not None
    return info


@mcp.tool()
def get_image_preview(
    image_path: str, max_size: int = 1200, save: bool = False, open_viewer: bool = False,
    region: Optional[list[float]] = None,
) -> list:
    """Render the image with all current edits applied and return it for analysis.

    By default nothing is written to disk and no window is opened.
    *save*: also write a __preview.jpg next to the source.
    *open_viewer*: save the preview and open it in the Windows image viewer (implies *save*).
    *max_size* controls the longest edge of the preview in pixels (default 1200).
    *region*: [x, y, w, h] normalised to the current (cropped) output, e.g. [0.4, 0.3, 0.15, 0.15]; only that
    part is rendered, at full resolution as long as it fits *max_size* (the result's "scale": 1.0 = 100 %
    view).  Use it to check halos, crunchy texture and noise, which downscaled previews hide.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return [f"Error: {e}"]

    img_bytes, info = _quick_preview(path, _region_state(state, region), max_size)
    if region:
        from PIL import Image as PILImage
        crop = _region_state(state, region).crop
        full_w = _full_size(path)[0] * (crop.right - crop.left)
        info["scale"] = round(PILImage.open(io.BytesIO(img_bytes)).width / full_w, 2)
    msg = "Preview rendered (not saved)."
    if save or open_viewer:
        msg = f"Preview saved to: {_save_preview(path, img_bytes, open_viewer)}"
        if open_viewer:
            msg += "\nOpening in your image viewer now..."

    return [f"{msg}\n{json.dumps(info, indent=1)}", Image(data=img_bytes, format="jpeg")]


@mcp.tool()
def apply_adjustments(
    image_path: str,
    # Exposure
    exposure_ev: Optional[float] = None,
    black_level: Optional[float] = None,
    highlight_recovery: Optional[float] = None,
    shadow_lift: Optional[float] = None,
    # White balance
    temperature_kelvin: Optional[float] = None,
    tint: Optional[float] = None,
    # Tone
    contrast: Optional[float] = None,
    brightness: Optional[float] = None,
    highlights: Optional[float] = None,
    shadows: Optional[float] = None,
    whites: Optional[float] = None,
    blacks: Optional[float] = None,
    # Colour
    saturation: Optional[float] = None,
    vibrance: Optional[float] = None,
    # Detail
    sharpness: Optional[float] = None,
    noise_reduction: Optional[float] = None,
    # Effects
    vignette: Optional[float] = None,
    clarity: Optional[float] = None,
    dehaze_strength: Optional[float] = None,
    dehaze_distance: Optional[float] = None,
) -> dict:
    """Apply one or more non-destructive adjustments to an image.

    Only the parameters you supply are changed; the rest keep their current values.
    Call get_image_preview afterwards to see the result.
    For RAWs rendered by darktable-cli, parameters marked [not rendered] are stored but have no effect;
    the result lists them under stored_but_not_rendered.

    Parameters
    ----------
    exposure_ev : float
        Exposure in stops (-5 to +5), relative to darktable's default.  0 = darktable's own starting
        brightness for RAWs: +0.7 EV plus compensation of the camera's exposure bias (e.g. a shot at -1 EV
        gets +1 EV back).  So +1 means 1 EV brighter than darktable's default, not than the raw file.
    black_level : float
        Lift or crush the black point (0.0–0.5).
    highlight_recovery : float
        [not rendered] Recover blown highlights (0.0–1.0).
    shadow_lift : float
        [not rendered] Open up dark shadows (0.0–1.0).
    temperature_kelvin : float
        Light source to neutralise (1667–25000 K): higher = warmer image.  ~5500 K is daylight, ~3200 K is
        tungsten.  Unset = as shot.  darktable color calibration illuminant.
    tint : float
        Positive = greener, negative = more magenta (-100 to +100, ±100 = 0.02 Duv).
        Only applied together with temperature_kelvin.
    contrast : float
        Global contrast around middle grey (-100 to +100), at constant colour.  darktable color balance
        rgb contrast, applied before the workflow's tone mapper (sigmoid / filmic / AgX).  On bright or
        backlit images positive values push highlights into the tone mapper's compression and clip them
        (a backlit test shot went from 0.2 % to 6 % clipped at +50, 0.6 % at +25): check get_histogram,
        and prefer a smaller value or highlights < 0 (which also darkens a high-key frame overall).
    brightness : float
        [not rendered] Global brightness (-100 to +100).
    highlights : float
        Darken (negative) or brighten (positive) highlights (-100 to +100 = ±1.5 EV in the brightest bands).
        darktable tone equalizer.
    shadows : float
        Lift (positive) or deepen (negative) shadows (-100 to +100 = ±1.5 EV in the darkest bands).
        darktable tone equalizer.
    whites : float
        [not rendered] White-point adjustment (-100 to +100).
    blacks : float
        [not rendered] Black-point adjustment (-100 to +100).
    saturation : float
        Colourfulness at constant brightness (-100 = monochrome, +100 = strong, about +60 % chroma).
        darktable color balance rgb global chroma.
    vibrance : float
        Colourfulness, slightly favouring muted colours (-100 to +100).  color balance rgb global vibrance.
    sharpness : float
        Sharpening strength (0 to 100).
    noise_reduction : float
        Noise reduction strength (0 to 100).
    vignette : float
        [not rendered] Vignette: negative values darken edges, positive lighten (-100 to +100).
    clarity : float
        Local contrast / clarity (-100 to +100).
    dehaze_strength : float
        darktable "haze removal" strength (-1 to +1, GUI default 0.2). 0 = off; negative adds haze.
        Only useful on genuinely hazy/foggy scenes; it is not a contrast tool.
    dehaze_distance : float
        How deep into the scene haze is removed (0 = foreground only, 1 = whole image; default 0.2).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    params = {k: v for k, v in {
        "exposure_ev": exposure_ev,
        "black_level": black_level,
        "highlight_recovery": highlight_recovery,
        "shadow_lift": shadow_lift,
        "temperature_kelvin": temperature_kelvin,
        "tint": tint,
        "contrast": contrast,
        "brightness": brightness,
        "highlights": highlights,
        "shadows": shadows,
        "whites": whites,
        "blacks": blacks,
        "saturation": saturation,
        "vibrance": vibrance,
        "sharpness": sharpness,
        "noise_reduction": noise_reduction,
        "vignette": vignette,
        "clarity": clarity,
        "dehaze_strength": dehaze_strength,
        "dehaze_distance": dehaze_distance,
    }.items() if v is not None}

    state.update(params)
    state.save()

    notes = _darktable_notes(path, state)
    ignored = params if "foreign_sidecar" in notes else notes.get("ignored_by_darktable", {})
    return {"status": "ok", "applied": [k for k in params if k not in ignored],
            "stored_but_not_rendered": [k for k in params if k in ignored]} | notes | {"edits": state.to_dict()}


@mcp.tool()
def crop_image(
    image_path: str,
    left: Optional[float] = None,
    top: Optional[float] = None,
    right: Optional[float] = None,
    bottom: Optional[float] = None,
    aspect_ratio: Optional[str] = None,
) -> dict:
    """Crop an image.

    Provide either:
    - *left*, *top*, *right*, *bottom* as normalised coordinates (0.0–1.0), or
    - *aspect_ratio* as a string like "16:9", "4:3", "1:1", "3:2" for a centre crop.

    Both methods can be combined (apply aspect ratio first, then fine-tune coordinates).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    crop = state.crop or CropState()

    if aspect_ratio:
        try:
            w_ratio, h_ratio = (float(x) for x in aspect_ratio.split(":"))
        except ValueError:
            return {"error": f"Invalid aspect_ratio format '{aspect_ratio}'. Use e.g. '16:9'."}

        # Current crop area dimensions (in normalised space)
        cw = crop.right - crop.left
        ch = crop.bottom - crop.top
        centre_x = crop.left + cw / 2
        centre_y = crop.top + ch / 2

        target_ar = w_ratio / h_ratio
        current_ar = cw / ch if ch > 0 else 1.0

        if current_ar > target_ar:
            new_w = ch * target_ar
            new_h = ch
        else:
            new_w = cw
            new_h = cw / target_ar

        crop.left = max(0.0, centre_x - new_w / 2)
        crop.top = max(0.0, centre_y - new_h / 2)
        crop.right = min(1.0, centre_x + new_w / 2)
        crop.bottom = min(1.0, centre_y + new_h / 2)

    if left is not None:
        crop.left = max(0.0, float(left))
    if top is not None:
        crop.top = max(0.0, float(top))
    if right is not None:
        crop.right = min(1.0, float(right))
    if bottom is not None:
        crop.bottom = min(1.0, float(bottom))

    if crop.right <= crop.left or crop.bottom <= crop.top:
        return {"error": "Invalid crop: right must be > left and bottom must be > top."}

    state.crop = crop
    state.save()

    return {"status": "ok", "crop": {
        "left": crop.left, "top": crop.top,
        "right": crop.right, "bottom": crop.bottom,
        "rotation": crop.rotation,
    }}


@mcp.tool()
def rotate_image(image_path: str, degrees: float) -> dict:
    """Rotate / straighten the image by *degrees* (clockwise).

    Positive = clockwise, negative = counter-clockwise.
    Typical use: small corrections like +0.5 or -1.2 to straighten horizons.
    Large rotations (90, 180, 270) are also supported.
    Not rendered yet for RAWs exported via darktable-cli (stored only; the result warns).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    crop = state.crop or CropState()
    crop.rotation = degrees
    state.crop = crop
    state.save()

    return {"status": "ok", "rotation_degrees": degrees} | _darktable_notes(path, state)


@mcp.tool()
def reset_crop(image_path: str) -> dict:
    """Remove all crop and rotation from the image, restoring the full frame."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    state.crop = None
    state.save()
    return {"status": "ok", "message": "Crop reset to full frame."}


@mcp.tool()
def graduated_filter(
    image_path: str,
    density: Optional[float] = None,
    hardness: Optional[float] = None,
    rotation: Optional[float] = None,
    offset: Optional[float] = None,
    hue: Optional[float] = None,
    saturation: Optional[float] = None,
    remove: bool = False,
) -> dict:
    """Graduated filter: darktable "graduated density", a simulated graduated ND filter (e.g. darken a bright sky).

    Only the parameters you supply change; the first call starts from darktable's "neutral gray ND2 (soft)"
    preset (density 1, hardness 0, rotation 0, offset 50).  *remove* deletes the filter.
    The gradient is placed on the full uncropped frame, as in darktable.

    density : float
        -8 to +8 EV.  Positive darkens the side *rotation* points to; negative brightens the OPPOSITE side.
        Not a full EV step: at the frame edge 1 EV gives about 0.67x, 2 EV 0.4x (×1.5 for -1 EV).
    hardness : float
        0 = soft (transition spans the whole frame) to 100 = hard edge (darktable presets use 0 or 75).
    rotation : float
        Degrees as darktable's slider shows them (-180 to 180): 0 = darkens the top, 90 = right, -90 = left,
        180 = bottom.
    offset : float
        Position of the transition line in % (50 = through the centre; at rotation 0, % from the top edge).
    hue, saturation : float
        Tint of the filter: hue 0–360°, saturation 0–100 % (0 = neutral grey).  darktable's orange preset is
        hue 37, saturation 80.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    if remove:
        state.graduated = None
    else:
        state.graduated = state.graduated or GraduatedState()
        state.graduated.__dict__.update({k: float(v) for k, v in {
            "density": density, "hardness": hardness, "rotation": rotation, "offset": offset,
            "hue": hue, "saturation": saturation}.items() if v is not None})
    state.save()
    return {"status": "ok", "edits": state.to_dict()} | _darktable_notes(path, state)


@mcp.tool()
def tone_mapper(
    image_path: str,
    contrast: Optional[float] = None,
    skew: Optional[float] = None,
    white_target: Optional[float] = None,
    black_target: Optional[float] = None,
    hue_preservation: Optional[float] = None,
    reset: bool = False,
) -> dict:
    """darktable sigmoid, the tone mapper of the user's scene-referred (sigmoid) workflow, in its GUI's units.

    Only the parameters you supply change, starting from darktable's own default; *reset* goes back to it.
    Punch through the tone mapper rolls highlights off smoothly, where apply_adjustments' contrast (color
    balance rgb, before the tone mapper) pushes bright frames into clipping.

    contrast : float
        Middle grey contrast (darktable default 1.5, slider soft range 0.7–3).  1.7–2.0 = punchy "summer day".
    skew : float
        -1 to 1 (default 0): negative leaves more room for highlights (softer roll-off, brighter shadows),
        positive for shadows (deeper shadows, highlights reach white sooner).
    white_target, black_target : float
        Display white / black in % (defaults 100 and 0.0152).  white < 100 dims the brightest tones.
    hue_preservation : float
        % (default 100).  Lower lets very bright saturated colours shift towards white/yellow, like film.
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    state.sigmoid = None if reset else state.sigmoid or SigmoidState()
    if state.sigmoid:
        state.sigmoid.__dict__.update({k: float(v) for k, v in {
            "contrast": contrast, "skew": skew, "white_target": white_target, "black_target": black_target,
            "hue_preservation": hue_preservation}.items() if v is not None})
    state.save()
    return {"status": "ok", "edits": state.to_dict()} | _darktable_notes(path, state)


@mcp.tool()
def color_equalizer(
    image_path: str,
    saturation: Optional[dict] = None,
    hue: Optional[dict] = None,
    brightness: Optional[dict] = None,
    reset: bool = False,
) -> dict:
    """darktable color equalizer: saturation, brightness and hue shift per hue, in its GUI's units.

    Each argument maps hue nodes red, orange, yellow, green, cyan, blue, lavender, magenta to a value; only the
    nodes you pass change, *reset* removes the module.  Foliage sits between yellow and green, skin is orange,
    blue sky is blue (cyan towards the horizon).
    saturation, brightness : % from -100 to +100 (0 = unchanged).  E.g. juicier greens like a camera JPEG:
        saturation {"green": 20, "yellow": 10}; a darker blue sky like a polariser, clouds untouched:
        brightness {"blue": -25}.
    hue : degrees from -180 to 180, shifts that node's colours (e.g. {"green": -10} warms foliage).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    if bad := [k for d in (saturation, hue, brightness) for k in d or {} if k not in HUES]:
        return {"error": f"Unknown hue node(s) {bad}; use {list(HUES)}"}
    state.color_eq = None if reset else state.color_eq or ColorEqualizerState()
    if state.color_eq:
        for field_, values in (("saturation", saturation), ("hue", hue), ("brightness", brightness)):
            getattr(state.color_eq, field_).update({k: float(v) for k, v in (values or {}).items()})
    state.save()
    return {"status": "ok", "edits": state.to_dict()} | _darktable_notes(path, state)


_MASK_DOC = """
    Masked adjustment: an extra darktable instance of each module the adjustments map to, blended through its
    own mask (drawn & parametric, blend colour space RGB (scene)), on top of the global edit.

    adjustments : dict
        Same names and scales as apply_adjustments: exposure_ev, black_level, contrast, highlights, shadows,
        saturation, vibrance, clarity, sharpness, dehaze_strength, dehaze_distance.  exposure_ev here is
        relative to the image the instance receives (no +0.7 EV default).
    mask : dict, any combination of (all present parts must match: drawn × parametric):
        gradient : {rotation, offset, compression}
            Drawn gradient on the full uncropped frame, upright as displayed (portrait shots included).
            rotation in degrees: 0 = mask on the top, 90 = left, -90 = right, 180 = bottom.
            offset = % position of the transition line from that edge (50 = centre).
            compression = darktable's value in %: a sigmoid (erf) transition whose visible part spans about
            3 × compression % of the frame diagonal and which is exactly 0 beyond 4 × compression %.  On 3:2
            that is ~3.5 × compression % of the long side, ~5 × of the short one (portrait, top/bottom: 3 → ~10 %
            of the height, 8 → ~30 %).  2–5 = short transition; darktable's default 50 = the whole frame.
            Confirm placement and width with export_mask_preview.
        luminance : [from, full_from, full_to, to]
            darktable's "g" (gray) slider in %: the mask is 0 below *from*, rises to 1 at *full_from*, stays 1
            to *full_to* and falls to 0 at *to*; 100 as the last two = no upper limit.  Linear values of the
            module's INPUT: before the tone mapper (exposure, shadows/highlights, contrast, saturation,
            vibrance, dehaze) middle grey is ~18 and highlights/clouds are often 50–200+; after it (clarity,
            sharpness) the range is display-referred 0–100.
        hue : [from, full_from, full_to, to]
            darktable's "hz" (JzCzhz hue) slider in degrees.  Approximate: red 40, orange/skin 60, yellow 100,
            green/foliage 120–135, cyan 205, sky/blue 250–260, magenta 320.  from > to wraps through 0 (reds).
        chroma : [from, full_from, full_to, to]
            darktable's "Cz" slider as shown (0 = grey, about 0.2+ = saturated, default boost).
        invert : bool — invert the whole mask.
        feathering_radius : px, 0–250, guided filter that makes the mask follow image edges (the key to
            invisible transitions on parametric masks; try 10–50).
        feathering_guide : "input after blur" (default), "input before blur", "output after blur",
            "output before blur".
        blur_radius : px, 0–100, Gaussian blur of the mask.
        opacity : %, 0–100, strength of the whole masked adjustment.
        contrast, brightness : %, -100 to 100, darktable's "mask contrast" and "mask opacity" refinement.
    Check the mask with export_mask_preview before judging the result.
"""


def _masked_reply(path: Path, state: EditState, m: MaskedAdjustment) -> dict:
    return {"status": "ok", "id": m.id, "masked_adjustment": state.to_dict()["masked"][state.masked.index(m)]} \
        | _darktable_notes(path, state)


def _set_masked(m: MaskedAdjustment, adjustments: Optional[dict], mask: Optional[dict]) -> Optional[str]:
    """Merge *adjustments* / *mask* into *m*; an error message if a name or value is not accepted."""
    if bad := [k for k in adjustments or {} if k not in MASKABLE]:
        return f"Not maskable: {bad}. Use: {list(MASKABLE)}"
    try:
        new_mask = MaskState(**({**asdict(m.mask), **mask} if mask else asdict(m.mask)))
    except TypeError as e:
        return f"Invalid mask: {e}"
    if new_mask.feathering_guide not in ("input after blur", "input before blur", "output after blur",
                                         "output before blur"):
        return f"Invalid feathering_guide: {new_mask.feathering_guide}"
    if any(r is not None and len(r) != 4 for r in (new_mask.luminance, new_mask.hue, new_mask.chroma)):
        return "luminance, hue and chroma take four values: [from, full_from, full_to, to]"
    m.adjustments |= adjustments or {}
    m.mask = new_mask
    return None


def add_masked_adjustment(image_path: str, adjustments: dict, mask: dict, label: Optional[str] = None) -> dict:
    """Apply adjustments only where a mask selects (e.g. clarity in the sky only, saturation of blues only).

    Returns the new adjustment's *id* for update/remove/export_mask_preview.  *label* names the instance in
    darktable (default: the id).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    mid = f"m{max([int(m.id[1:]) for m in state.masked] + [0]) + 1}"
    m = MaskedAdjustment(mid, label or mid, {}, MaskState())
    if err := _set_masked(m, adjustments, mask):
        return {"error": err}
    state.masked.append(m)
    state.save()
    return _masked_reply(path, state, m)


add_masked_adjustment.__doc__ += _MASK_DOC
mcp.tool()(add_masked_adjustment)


def update_masked_adjustment(image_path: str, id: str, adjustments: Optional[dict] = None,
                             mask: Optional[dict] = None, label: Optional[str] = None) -> dict:
    """Change a masked adjustment: only the adjustments and mask fields you pass change (a mask "gradient"
    is replaced whole; pass "gradient": null to drop it)."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    if not (m := next((m for m in state.masked if m.id == id), None)):
        return {"error": f"No masked adjustment {id!r}", "ids": [m.id for m in state.masked]}
    if err := _set_masked(m, adjustments, mask):
        return {"error": err}
    m.label = label or m.label
    state.save()
    return _masked_reply(path, state, m)


update_masked_adjustment.__doc__ += _MASK_DOC
mcp.tool()(update_masked_adjustment)


@mcp.tool()
def remove_masked_adjustment(image_path: str, id: str) -> dict:
    """Delete a masked adjustment."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    state.masked = [m for m in state.masked if m.id != id]
    state.save()
    return {"status": "ok", "masked": state.to_dict()["masked"]}


@mcp.tool()
def list_masked_adjustments(image_path: str) -> dict:
    """List the masked adjustments of an image (id, label, adjustments, mask)."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    return {"masked": state.to_dict()["masked"]}


@mcp.tool()
def export_mask_preview(image_path: str, id: Optional[str] = None, max_size: int = 1200) -> list:
    """Render the masks darktable computes (white = full effect), one image per module instance, at the
    output crop.  *id*: only that masked adjustment's masks (default: all of them)."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return [f"Error: {e}"]
    if path.suffix.lower() not in RAW_EXTENSIONS or not DARKTABLE_CLI:
        return ["Error: mask previews need darktable-cli and a RAW file"]
    from PIL import Image as PILImage, ImageSequence
    labels = [m.label for m in state.masked if id in (None, m.id)]
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp, "masks.tif")
        result = _export_via_darktable_cli(path, dst, _render_xmp(path, state, tmp, False), 90, max_size, True)
        if result["status"] != "ok":
            return [f"Error: darktable-cli failed: {result}"]
        out = []
        with PILImage.open(dst) as tif:
            # darktable names each mask layer "<module> • <instance name>" (TIFF PageName, UTF-8)
            for page in ImageSequence.Iterator(tif):
                name = page.tag_v2.get(285, "").encode("latin-1").decode("utf-8", "replace")
                if any(name.endswith(f"\u2022 {label}") for label in labels):
                    buf = io.BytesIO()
                    page.convert("L").save(buf, format="JPEG", quality=90)
                    out += [f"Mask: {name}", Image(data=buf.getvalue(), format="jpeg")]
    return out or [f"No mask found for {id or 'any masked adjustment'}" + (
        "; the sidecar holds the user's own darktable edits, rendered as-is" if foreign_edits(path) else "")]


def _ncc(image, templ):
    """Normalised cross-correlation of *templ* at every offset inside *image* (2-D float arrays), via FFT."""
    import numpy as np
    (H, W), (h, w) = image.shape, templ.shape
    t = templ - templ.mean()
    t /= np.sqrt((t ** 2).sum())
    corr = np.fft.irfft2(np.fft.rfft2(image) * np.conj(np.fft.rfft2(t, image.shape)), image.shape)
    c1, c2 = (np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0))) for a in (image, image ** 2))
    win = lambda c: c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]
    return corr[:H - h + 1, :W - w + 1] / np.sqrt(np.maximum(win(c2) - win(c1) ** 2 / t.size, 1e-9))


@mcp.tool()
def match_crop_to_jpeg(raw_path: str, jpeg_path: str) -> dict:
    """Set the RAW's crop to the frame of an (edited, possibly cropped) camera JPEG of the same shot.

    Finds the JPEG inside a render of the uncropped RAW (normalised cross-correlation on luminance, so a
    different camera look does not matter) assuming the JPEG keeps the sensor's pixel scale (within ±4 %, which
    is also searched).  Keeps the current rotation.  Reports the offset and size in full-resolution pixels,
    the scale found and a match score (1 = identical structure; below ~0.6 the match is doubtful: check the
    preview).  The JPEG must not be rotated or resized relative to the camera's output.
    """
    import numpy as np
    from PIL import Image as PILImage, ImageOps
    try:
        path, state = _load_or_new(raw_path)
    except FileNotFoundError as e:
        return {"error": str(e)}
    full_w, full_h = _full_size(path)
    img, info = _render(path, replace(state, crop=None), 1000)
    image, f = np.asarray(img.convert("L"), float), img.width / full_w
    with PILImage.open(jpeg_path) as jpg:
        jpg = ImageOps.exif_transpose(jpg).convert("L")
        jpg = jpg.resize((round(jpg.width * f), round(jpg.height * f)), PILImage.Resampling.LANCZOS)
    best = (-1.0,)
    for k in np.arange(0.96, 1.041, 0.01):
        w, h = round(jpg.width * k), round(jpg.height * k)
        if w <= image.shape[1] and h <= image.shape[0]:
            m = _ncc(image, np.asarray(jpg.resize((w, h), PILImage.Resampling.LANCZOS), float))
            best = max(best, (m.max(), *np.unravel_index(m.argmax(), m.shape), k, w, h))
    if len(best) == 1:
        return {"error": "The JPEG is larger than the RAW render: it was resized, not just cropped"}
    score, y, x, k, w, h = map(float, best)
    W, H = image.shape[1], image.shape[0]
    state.crop = CropState(x / W, y / H, min(1.0, (x + w) / W), min(1.0, (y + h) / H),
                           state.crop.rotation if state.crop else 0.0)
    state.save()
    return {"status": "ok", "crop": asdict(state.crop), "offset_px": [round(x / f), round(y / f)],
            "size_px": [round(w / f), round(h / f)], "scale": round(float(k), 2), "score": round(float(score), 3),
            "precision_px": round(1 / f)} | info


@mcp.tool()
def compare_images(path_a: str, path_b: str, region: Optional[list[float]] = None, max_size: int = 1200) -> list:
    """One image with *path_a* (left) and *path_b* (right) side by side at the same height, each rendered with
    its own edits (RAWs as the export would; JPEGs/other images as they are, plus any edits made here).

    *region*: [x, y, w, h] normalised to each image's current output, applied to both (e.g. compare the same
    detail of the RAW render and the camera JPEG); rendered at up to *max_size* px per image.
    """
    from PIL import Image as PILImage
    try:
        rendered = [_render(p, _region_state(st, region), max_size) for p, st in map(_load_or_new, (path_a, path_b))]
    except FileNotFoundError as e:
        return [f"Error: {e}"]
    h = min(img.height for img, _ in rendered)
    a, b = (img.resize((round(img.width * h / img.height), h), PILImage.Resampling.LANCZOS) for img, _ in rendered)
    out = PILImage.new("RGB", (a.width + 8 + b.width, h), "white")
    out.paste(a), out.paste(b, (a.width + 8, 0))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=90)
    return [json.dumps({"left": path_a, "right": path_b, "a": rendered[0][1], "b": rendered[1][1]}, indent=1),
            Image(data=buf.getvalue(), format="jpeg")]


@mcp.tool()
def rename_output(image_path: str, new_name: str) -> dict:
    """Set the output filename stem (without extension) for the exported image.

    For example: rename_output("/photos/DSC001.NEF", "golden_hour_lake")
    will export as golden_hour_lake.jpg (or whichever format you choose in export_image).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    # Strip any extension the user may have included
    stem = Path(new_name).stem or new_name
    state.output_name = stem
    state.save()
    return {"status": "ok", "output_name": stem}


@mcp.tool()
def reset_edits(image_path: str) -> dict:
    """Reset all adjustments, crop, and output name — back to the unedited original."""
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    sidecar = path.with_name(path.stem + ".mcp.json")
    if sidecar.exists():
        sidecar.unlink()

    return {"status": "ok", "message": "All edits reset to original."}


@mcp.tool()
def export_image(
    image_path: str,
    output_directory: Optional[str] = None,
    format: str = "jpeg",
    quality: int = 92,
    use_darktable_cli: bool = True,
    write_xmp_sidecar: bool = True,
    max_dimension: Optional[int] = None,
    overwrite: bool = False,
) -> dict:
    """Export the edited image to a file.

    Parameters
    ----------
    image_path : str
        Source image path.
    output_directory : str, optional
        Destination folder.  Defaults to the same folder as the source.
    format : str
        Output format: "jpeg", "png", "tiff" (default "jpeg").
    quality : int
        JPEG quality 1–100 (default 92).  Ignored for PNG/TIFF.
    use_darktable_cli : bool
        Try to use darktable-cli for export when available (better RAW rendering).
    write_xmp_sidecar : bool
        Write a Darktable-compatible XMP sidecar alongside the source file. Either way the render uses the
        current edits, unless a sidecar written by darktable itself exists (that one is used and kept).
    max_dimension : int, optional
        Resize so the longest edge is at most this many pixels.
    overwrite : bool
        Replace an existing file of the same name (e.g. a named final export) instead of adding _1, _2, ….
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    out_dir = Path(output_directory) if output_directory else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = state.output_name or path.stem
    ext_map = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "tiff": ".tif", "tif": ".tif"}
    ext = ext_map.get(format.lower(), ".jpg")
    out_path = out_dir / (stem + ext)

    # darktable-cli never overwrites (it adds _01), and success is judged by the file appearing
    if overwrite:
        out_path.unlink(missing_ok=True)
    counter = 1
    while out_path.exists():
        out_path = out_dir / f"{stem}_{counter}{ext}"
        counter += 1

    is_raw = path.suffix.lower() in RAW_EXTENSIONS
    cli_error = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            xmp_path = _render_xmp(path, state, tmp, write_xmp_sidecar)
            saved = write_xmp_sidecar and not foreign_sidecar(path)
            xmp_info = {"xmp_sidecar": str(xmp_path) if saved else None}
            if write_xmp_sidecar and not saved and not foreign_edits(path):
                xmp_info["edits_not_saved"] = (
                    f"These edits were NOT saved to any XMP sidecar: {xmp_sidecar_path(path).name} was written by "
                    "darktable or another tool and is never overwritten. darktable will not show them; apart from "
                    "this server's .mcp.json, the exported image is the only record of them.")
        except Exception as exc:
            xmp_path, xmp_info = None, {"xmp_note": f"XMP write failed: {exc}"}
        if use_darktable_cli and DARKTABLE_CLI and is_raw:
            result = _export_via_darktable_cli(path, out_path, xmp_path, quality, max_dimension)
            if result.get("status") == "ok":
                return result | {"format": format} | xmp_info | _darktable_notes(path, state)
            cli_error = result

    proc = ImageProcessor(path)
    img = proc.process(state)

    if max_dimension:
        from PIL import Image as PILImage
        img.thumbnail((max_dimension, max_dimension), PILImage.Resampling.LANCZOS)

    save_kwargs: dict = {}
    if format.lower() in ("jpeg", "jpg"):
        save_kwargs = {"format": "JPEG", "quality": quality, "optimize": True}
    elif format.lower() == "png":
        save_kwargs = {"format": "PNG", "optimize": True}
    elif format.lower() in ("tiff", "tif"):
        save_kwargs = {"format": "TIFF"}

    img.save(out_path, **save_kwargs)

    return {
        "status": "ok",
        "output_path": str(out_path),
        "format": format,
        "size_bytes": out_path.stat().st_size,
        "rendered_via": "rawpy" if is_raw else "pillow",
    } | xmp_info | ({"darktable_cli_error": cli_error} if cli_error else {})


def _export_via_darktable_cli(
    src: Path, dst: Path, xmp: Optional[Path], quality: int, max_dimension: Optional[int], masks: bool = False
) -> dict:
    # Throwaway configdir seeded with the user's darktablerc (workflow) and data.db (presets/styles);
    # in-memory library so the user's library.db is never touched.
    with tempfile.TemporaryDirectory() as cfg:
        user_cfg = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "darktable"
        for name in ("darktablerc", "data.db"):
            if (user_cfg / name).exists():
                shutil.copy2(user_cfg / name, cfg)
        size = str(max_dimension or 0)
        # darktable-cli drops backslashes, so pass forward-slash paths
        cmd = [DARKTABLE_CLI, src.as_posix(), *([xmp.as_posix()] if xmp else []), dst.as_posix(),
               "--width", size, "--height", size, "--hq", "true", "--export_masks", str(masks).lower(),
               "--core", "--configdir", Path(cfg).as_posix(), "--library", ":memory:",
               "--conf", f"plugins/imageio/format/jpeg/quality={quality}"]
        try:
            # darktable-cli exits 0 even on bad arguments, so success = output file exists
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as exc:
            return {"status": "error", "exception": str(exc)}
    if dst.exists():
        return {"status": "ok", "output_path": str(dst), "rendered_via": "darktable-cli",
                "size_bytes": dst.stat().st_size}
    return {"status": "error", "returncode": result.returncode, "output": (result.stderr or result.stdout)[-500:]}


@mcp.tool()
def get_histogram(image_path: str) -> dict:
    """Compute a brightness/channel histogram for the image (with current edits).

    Returns per-channel (R, G, B) and luminance histograms as 256-bin arrays.
    Useful for analysing exposure, clipping, and tonal distribution.
    """
    import numpy as np

    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    img, info = _render(path, state, 800)
    arr = np.array(img, dtype=np.uint8)

    def _hist(channel: np.ndarray) -> list[int]:
        counts, _ = np.histogram(channel.ravel(), bins=256, range=(0, 256))
        return counts.tolist()

    r_hist = _hist(arr[..., 0])
    g_hist = _hist(arr[..., 1])
    b_hist = _hist(arr[..., 2])
    lum = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]).astype(np.uint8)
    lum_hist = _hist(lum)

    clipped_highlights = int((arr == 255).all(axis=2).sum())
    clipped_shadows = int((arr == 0).all(axis=2).sum())
    total_pixels = arr.shape[0] * arr.shape[1]

    return {
        "r": r_hist,
        "g": g_hist,
        "b": b_hist,
        "luminance": lum_hist,
        "clipped_highlights_px": clipped_highlights,
        "clipped_shadows_px": clipped_shadows,
        "total_pixels": total_pixels,
        "highlight_clip_pct": round(clipped_highlights / total_pixels * 100, 2),
        "shadow_clip_pct": round(clipped_shadows / total_pixels * 100, 2),
    } | info


@mcp.tool()
def copy_settings(source_image_path: str, target_image_path: str) -> dict:
    """Copy all edit settings (adjustments, crop, graduated filter, tone mapper, color equalizer, masked adjustments) from one image to another.

    Useful for batch-editing a set of photos shot under the same conditions.
    The output_name is NOT copied — each image keeps its own name.
    """
    try:
        src_path, src_state = _load_or_new(source_image_path)
        tgt_path, tgt_state = _load_or_new(target_image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    from copy import deepcopy
    tgt_state.adjustments = replace(src_state.adjustments)
    tgt_state.crop = replace(src_state.crop) if src_state.crop else None
    tgt_state.graduated = replace(src_state.graduated) if src_state.graduated else None
    tgt_state.sigmoid = replace(src_state.sigmoid) if src_state.sigmoid else None
    tgt_state.color_eq = deepcopy(src_state.color_eq)
    tgt_state.masked = deepcopy(src_state.masked)
    tgt_state.save()

    return {"status": "ok", "copied_to": str(tgt_path), "edits": tgt_state.to_dict()}
