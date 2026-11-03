"""Write darktable 5.x XMP sidecars (``IMG_x.CR3.xmp``) from an EditState.

Param blobs are little-endian C structs whose layouts were decoded from real
darktable 5.6.1 sidecars and confirmed by rendering with darktable-cli.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

from .edits import EditState

_BLENDOP = "gz11eJxjYIAACQYYOOHEgAZY0QWAgBGLGANDgz0Ej1Q+dcF/IADRAGpyHQU="
_BLENDOP_EXPOSURE = "gz08eJxjYGBgYAFiCQYYOOHEgAZY0QWAgBGLGANDgz0Ej1Q+dlAx68oBEMbFxwX+AwGIBgCbGCeh"
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
        mods.append(("exposure", 7, params.hex(), _SCENE_DEFAULT, _BLENDOP_EXPOSURE))
    if adj.dehaze_strength:
        # hazeremoval v3: strength, distance, compatibility_mode=FALSE, adaptive=TRUE (5.6.1 defaults)
        params = struct.pack("<2f2i", adj.dehaze_strength, adj.dehaze_distance, 0, 1)
        mods.append(("hazeremoval", 3, params.hex(), "", _BLENDOP))
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
