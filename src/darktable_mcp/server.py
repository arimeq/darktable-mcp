"""Darktable MCP Server — photo editing tools for Claude."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import Image, MCPServer

from .edits import CropState, EditState
from .processor import ALL_EXTENSIONS, RAW_EXTENSIONS, ImageProcessor
from .xmp_writer import foreign_edits, foreign_sidecar, ignored_by_darktable, xmp_sidecar_path, xmp_text

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
    image_path: str, max_size: int = 1200, save: bool = False, open_viewer: bool = False
) -> list:
    """Render the image with all current edits applied and return it for analysis.

    By default nothing is written to disk and no window is opened.
    *save*: also write a __preview.jpg next to the source.
    *open_viewer*: save the preview and open it in the Windows image viewer (implies *save*).
    *max_size* controls the longest edge of the preview in pixels (default 1200).
    """
    try:
        path, state = _load_or_new(image_path)
    except FileNotFoundError as e:
        return [f"Error: {e}"]

    img_bytes, info = _quick_preview(path, state, max_size)
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

    # Avoid collision
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
    src: Path, dst: Path, xmp: Optional[Path], quality: int, max_dimension: Optional[int]
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
               "--width", size, "--height", size, "--hq", "true",
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
    """Copy all edit settings (adjustments + crop) from one image to another.

    Useful for batch-editing a set of photos shot under the same conditions.
    The output_name is NOT copied — each image keeps its own name.
    """
    try:
        src_path, src_state = _load_or_new(source_image_path)
        tgt_path, tgt_state = _load_or_new(target_image_path)
    except FileNotFoundError as e:
        return {"error": str(e)}

    from dataclasses import replace, asdict
    tgt_state.adjustments = replace(src_state.adjustments)
    tgt_state.crop = replace(src_state.crop) if src_state.crop else None
    tgt_state.save()

    return {"status": "ok", "copied_to": str(tgt_path), "edits": tgt_state.to_dict()}
