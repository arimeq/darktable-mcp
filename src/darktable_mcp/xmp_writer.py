"""Write darktable 5.x XMP sidecars (``IMG_x.CR3.xmp``) from an EditState.

Param blobs are little-endian C structs whose layouts were decoded from real
darktable 5.6.1 sidecars and confirmed by rendering with darktable-cli.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Optional

from .edits import EditState

_BLENDOP = "gz11eJxjYIAACQYYOOHEgAZY0QWAgBGLGANDgz0Ej1Q+dcF/IADRAGpyHQU="
# Blending off, blend colour space "RGB (scene)" as darktable assigns to scene-referred RGB modules.
_BLENDOP_SCENE = "gz08eJxjYGBgYAFiCQYYOOHEgAZY0QWAgBGLGANDgz0Ej1Q+dlAx68oBEMbFxwX+AwGIBgCbGCeh"
# Same multi_name as darktable's auto-applied instance, so ours replaces it instead of stacking.
# Same multi_name as darktable's auto-applied instance, so ours replaces it instead of stacking.
_SCENE_DEFAULT = "_builtin_scene-referred default"
_DT_DEFAULT_EXPOSURE = 0.7

_FILMIC_FLOATS = [18.45, -7.4833, 4.8267, 0, 3, 100, 100, 0, 0, 18.45, 0.0152, 100, 3.3956, 0.01, 1.0, 0, 0, 0.2]
_FILMIC_INTS = [3, 4, 1, 0, 1, 1, 0, 0, 0, 2, 0]

# Blob of darktable's built-in preset "_builtin_wavelets: chroma only" (denoiseprofile v12).
_DENOISE_CHROMA = (
    "0000803f0000e0409a99993f000000000000000000000000cdcccc3d0000803f000080bf"
    "00000000000000000000000000000000000000000100000000000000"
    + "abaa2a3eabaaaa3e0000003fabaa2a3f5655553f0000803f00000000" * 5
    + "abaa2a3eabaaaa3e0000003fabaa2a3f5655553f0000803f"
    + "0000003f" * 28 + "00000000" * 7 + "0000003f" * 7
    + "01000000" * 4 + "00000000"
)

_HEADER = """\
<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:darktable="http://darktable.sf.net/"
   xmpMM:DerivedFrom="{source}"
   darktable:xmp_version="5"
   darktable:raw_params="0"
   darktable:auto_presets_applied="0"
   darktable:history_end="{end}"
   darktable:iop_order_version="4">
   <darktable:masks_history>
    <rdf:Seq/>
   </darktable:masks_history>
   <darktable:history>
    <rdf:Seq>
{items}    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""

_ITEM = """\
     <rdf:li
      darktable:num="{num}"
      darktable:operation="{op}"
      darktable:enabled="1"
      darktable:modversion="{ver}"
      darktable:params="{params}"
      darktable:multi_name="{name}"
      darktable:multi_name_hand_edited="0"
      darktable:multi_priority="0"
      darktable:blendop_version="14"
      darktable:blendop_params="{blendop}"/>
"""


def _locus_xy(t: float) -> tuple[float, float]:
    """darktable's CCT_to_xy_daylight (>= 4000 K) / CCT_to_xy_blackbody (common/illuminants.h)."""
    if t >= 4000:
        x = (((-4.6070e9 / t + 2.9678e6) / t + 0.09911e3) / t + 0.244063 if t <= 7000
             else ((-2.0064e9 / t + 1.9018e6) / t + 0.24748e3) / t + 0.237040)
        return x, (-3 * x + 2.87) * x - 0.275
    x = ((-0.2661239e9 / t - 0.2343589e6) / t + 0.8776956e3) / t + 0.179910
    if t <= 2222:
        return x, ((-1.1063814 * x - 1.34811020) * x + 2.18555832) * x - 0.20219683
    return x, ((-0.9549476 * x - 1.37418593) * x + 2.09137015) * x - 0.16748867


def _color_calibration(kelvin: float, tint: float) -> bytes:
    """channelmixerrgb v3 with identity mixing and a CAT16 adaptation to the given illuminant.

    Like Lightroom's slider, kelvin is the light being neutralised: higher = warmer image. Without tint it is
    darktable's daylight (>= 4000 K) or black-body illuminant; tint moves it off that locus along the CIE 1960
    normal, ±100 tint = ∓0.02 Duv (positive tint = magenta illuminant = greener image), as a custom x/y illuminant.
    """
    t = min(max(kelvin, 1667.0), 25000.0)
    x, y = _locus_xy(t)
    illuminant = 2 if t >= 4000 else 6  # DT_ILLUMINANT_D / DT_ILLUMINANT_BB
    if tint:
        def uv(t):
            x, y = _locus_xy(t)
            return 4 * x / (-2 * x + 12 * y + 3), 6 * y / (-2 * x + 12 * y + 3)
        # tangent on the same side of the 4000 K daylight/black-body seam
        (u0, v0), (u1, v1) = (uv(t), uv(t + 20)) if t >= 4000 else (uv(t - 20), uv(t))
        n = math.hypot(u1 - u0, v1 - v0)
        nu, nv = -(v1 - v0) / n, (u1 - u0) / n
        if nv < 0:  # point the normal to the green side of the locus
            nu, nv = -nu, -nv
        u, v = uv(t)
        u, v = u - tint / 100 * 0.02 * nu, v - tint / 100 * 0.02 * nv
        x, y, illuminant = 3 * u / (2 * u - 8 * v + 4), 2 * v / (2 * u - 8 * v + 4), 7  # DT_ILLUMINANT_CUSTOM
    identity = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
    # red/green/blue rows, saturation/lightness/grey 0; normalize flags off; illuminant, F3, LED B5, CAT16;
    # x, y, temperature, gamut 1, clip TRUE, saturation algorithm v3
    return struct.pack("<24f6i4i4f2i", *identity, *[0] * 12, *[0] * 6, illuminant, 2, 4, 1, x, y, t, 1.0, 1, 2)


def _tone_equalizer(shadows: float, highlights: float) -> bytes:
    """toneequal v2 using the mask of darktable's "compress shadows/highlights | EIGF | medium" preset.

    That mask is shifted -1.57 EV so middle grey sits on the -4 EV band. Shadows drive the -8..-5 EV bands and
    highlights the -3..0 EV bands with weights 0.6/1/1/0.6 (the preset's own shape: the outermost band, mostly
    noise or speculars, moves less); -4 EV stays put. ±100 = ±1.5 EV at the strongest bands; darktable's
    Gaussian band interpolation keeps the curve smooth.
    """
    s, h, w = shadows / 100 * 1.5, highlights / 100 * 1.5, (0.6, 1.0, 1.0, 0.6)
    bands = [s * k for k in w] + [0.0] + [h * k for k in w]
    # blending 3, smoothing √2, feathering 7, quantization 0, mask contrast 0, mask exposure -1.57;
    # details EIGF, luminance estimator RGB euclidean norm, 3 iterations
    return struct.pack("<15f3i", *bands, 3.0, math.sqrt(2), 7.0, 0.0, 0.0, -1.57, 4, 4, 3)


def _color_balance(saturation: float, vibrance: float) -> bytes:
    """colorbalancergb v5, neutral except global chroma and global vibrance.

    Global chroma, not global saturation: chroma scales colourfulness at constant luminance and is computed in
    the same step as vibrance; saturation is darktable's perceptual one that also darkens what it saturates.
    Scale: -100 → chroma -1 (monochrome), +100 → +0.5; vibrance ±100 → ±0.5. 0.5 is the end of darktable's own
    slider soft range, where the effect is strong but still usable.
    """
    f = [0.0] * 32
    f[12] = f[14] = 1.0  # shadows / highlights fall-off
    f[17] = saturation / 100 * (0.5 if saturation > 0 else 1.0)  # chroma_global
    f[28] = f[30] = 0.1845  # mask / contrast grey fulcrum
    f[29] = vibrance / 100 * 0.5
    return struct.pack("<32fi", *f, 1)  # saturation formula: darktable UCS (module default)


def xmp_sidecar_path(src: Path) -> Path:
    return src.with_name(src.name + ".xmp")


def write_xmp(edit_state: EditState) -> Optional[Path]:
    """Write the sidecar and return its path, or None if one written by darktable or another tool exists."""
    text = xmp_text(edit_state)
    if text is None:
        return None
    xmp_path = xmp_sidecar_path(edit_state.source_path)
    xmp_path.write_text(text, encoding="utf-8")
    return xmp_path


def xmp_text(edit_state: EditState) -> Optional[str]:
    """Sidecar content, or None if an existing sidecar written by darktable or another tool must be kept."""
    src = edit_state.source_path
    xmp_path = xmp_sidecar_path(src)
    if xmp_path.exists():
        # darktable always stamps import_timestamp (exif.cc); we never do. Anything else isn't ours to overwrite.
        text = xmp_path.read_text(encoding="utf-8", errors="ignore")
        if "darktable:history" not in text or "import_timestamp" in text:
            return None

    adj, crop = edit_state.adjustments, edit_state.crop
    mods: list[tuple[str, int, str, str, str]] = []
    if adj.exposure_ev or adj.black_level:
        # darktable's black slider spans ±0.1 and 0.01 already crushes shadows visibly, so scale the 0–0.5 range down
        params = struct.pack("<i4f2i", 0, adj.black_level / 10,_DT_DEFAULT_EXPOSURE + adj.exposure_ev, 50, -4, 1, 1)
        mods.append(("exposure", 7, params.hex(), _SCENE_DEFAULT, _BLENDOP_SCENE))
    if adj.dehaze_strength:
        # hazeremoval v3: strength, distance, compatibility_mode=FALSE, adaptive=TRUE (5.6.1 defaults)
        params = struct.pack("<2f2i", adj.dehaze_strength, adj.dehaze_distance, 0, 1)
        mods.append(("hazeremoval", 3, params.hex(), "", _BLENDOP))
    if adj.temperature_kelvin is not None:
        # Same multi_name as the auto-applied "as shot" instance, so ours replaces it: one CAT on the pipe.
        mods.append(("channelmixerrgb", 3, _color_calibration(adj.temperature_kelvin, adj.tint).hex(),
                     _SCENE_DEFAULT, _BLENDOP_SCENE))
    if adj.shadows or adj.highlights:
        mods.append(("toneequal", 2, _tone_equalizer(adj.shadows, adj.highlights).hex(), "", _BLENDOP_SCENE))
    if adj.saturation or adj.vibrance:
        mods.append(("colorbalancergb", 5, _color_balance(adj.saturation, adj.vibrance).hex(), "", _BLENDOP_SCENE))
    if adj.contrast:
        floats = _FILMIC_FLOATS.copy()
        floats[14] = max(0.0, 1.0 + adj.contrast / 100)
        mods.append(("filmicrgb", 6, struct.pack("<18f11i", *floats, *_FILMIC_INTS).hex(), _SCENE_DEFAULT, _BLENDOP))
    if adj.clarity:
        mods.append(("bilat", 3, struct.pack("<i4f", 0, 50, 50, 1.0 + adj.clarity / 100, 0.2).hex(), "", _BLENDOP))
    if adj.sharpness > 0:
        mods.append(("sharpen", 1, struct.pack("<3f", 2.0, adj.sharpness / 100, 0.5).hex(), "", _BLENDOP))
    if adj.noise_reduction > 0:
        mods.append(("denoiseprofile", 12, _DENOISE_CHROMA, "", _BLENDOP))
    if crop:
        # crop v3: left, top, right, bottom (normalised), ratio_d/ratio_n 0 = freehand
        params = struct.pack("<4f2i", crop.left, crop.top, crop.right, crop.bottom, 0, 0)
        mods.append(("crop", 3, params.hex(), "", _BLENDOP))

    items = "".join(
        _ITEM.format(num=i, op=op, ver=ver, params=p, name=name, blendop=b)
        for i, (op, ver, p, name, b) in enumerate(mods)
    )
    return _HEADER.format(source=src.name, end=len(mods), items=items)
