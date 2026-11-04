# Proposal: masks and new tools for darktable-mcp

Status: proposal, to be implemented in phases. Target: darktable 5.6.1, rendering via `darktable-cli`.

## Why

darktable-cli renders whatever history stack the XMP contains — masks, multiple module instances, any module. The limit of what we can do is therefore **what this server can write into the XMP**, not the CLI.

A real editing session (Olympus ORF landscapes, 2026-11-04) hit the following walls:

- **Global-only adjustments.** On a funicular shot (P8192730) the sky needed local contrast (`clarity`) for crisp cloud edges, but the same clarity made forests, grass and hills look crunchy and over-drawn. The fix is clarity in the sky only — impossible today.
- **Gradient edge visible.** A soft graduated filter (hardness 30) ran its transition across half the frame and killed the depth of the landscape just above the horizon. A hard filter placed high worked, but the real need is a mask that follows *what* is bright (clouds), not a straight line.
- **Cloud detail vs. blue sky.** Recovering the brightest cloud with a grey ND also greyed the blue patches. Needed: saturation/brightness of *blue only*.
- **Contrast clips highlights.** `contrast` goes through color balance rgb before the tone mapper and pushes bright frames into clipping (the tool description itself warns about it). The user likes fairly strong, "warm summer day" contrast.
- **Camera-JPEG greens.** On P8192711 the user preferred the Olympus JPEG: darktable's forest green looked washed out. Needed: per-hue saturation/brightness.
- **Halo/crunch detection.** At 900 px previews halos and over-sharpened texture were only visible at clarity +25; the user saw them immediately at full size.
- **Matching the camera-JPEG crop.** Three times the crop of the user's edited camera JPEG had to be reconstructed by hand from landmark offsets.

## Principles (keep as in the graduated filter work)

1. **darktable's own scales 1:1.** A value passed to the tool must be the value the user sees in the darktable GUI after loading the XMP. Document the real-world effect in the tool description instead of converting (as done for `graduated_filter` density).
2. **Verify against source and renders.** For every new XMP structure, check darktable 5.6.1 source (e.g. `src/develop/blend.h`, `src/develop/blend.c`, `src/develop/masks/*`, the module's `params` struct and version) and confirm with a `darktable-cli` render on a synthetic test image before relying on it.
3. **Don't break existing behaviour.** Existing tools, sidecar rules (own XMP vs. darktable-authored XMP) and `stored_but_not_rendered` reporting stay as they are. New state must round-trip through the server's XMP and be copied by `copy_settings`.
4. **Report what was rendered.** As now: `rendered_via`, warnings, and anything stored but not rendered.

## Phase 1 — masks (highest priority)

### Design

Masks should be a **general mechanism**, not a per-tool feature. In darktable almost every processing module supports blending/masking, and a module can have multiple instances. Proposed model:

- A **masked adjustment** = an extra *instance* of the relevant module (local contrast, color balance rgb, tone equalizer, exposure, …) with its own params and a mask in its blend params. Global adjustments keep using the base instance.
- API sketch (names are suggestions):

```
add_masked_adjustment(image_path, adjustments={...}, mask={...}, label="sky clarity") -> id
update_masked_adjustment(image_path, id, adjustments=?, mask=?)
remove_masked_adjustment(image_path, id)
list_masked_adjustments(image_path)
```

`adjustments` reuses the same parameter names as `apply_adjustments` (clarity, saturation, vibrance, exposure_ev, shadows, highlights, contrast, …). Each one maps to its module instance exactly as the global version does.

### Mask types

1. **Gradient (drawn mask).** Same geometry vocabulary as `graduated_filter`: `rotation`, `offset` (%), `hardness` / or darktable's gradient compression + steepness — whichever matches the GUI, documented. Placed on the full uncropped frame, like the filter.
2. **Parametric (blend-if).** Ranges on:
   - **luminance** (e.g. "only the brightest tones" for clouds),
   - **hue** (e.g. "only blue" for the sky, "only green" for forests),
   - **chroma** (optional).
   Each range with darktable's four-point (in / feather / feather / out) definition, plus invert.
3. **Combination** of drawn + parametric (darktable's "drawn & parametric" mode, intersection by default) — e.g. "bright tones, but only above the horizon".

### Mask refinement (essential — this decides whether edges show)

- feathering radius (guided filter) and its guide (input/output image),
- mask blur,
- mask opacity,
- mask contrast/brightness refinement if available in 5.6.1.

### Diagnostics for masks

- `export_mask_preview(image_path, id)` — render the mask itself as a grayscale image (darktable-cli has `--export_masks`, which stores masks as layers where the output format supports it, e.g. TIFF; alternatively render with a debug blend mode).

### Acceptance test (real image)

On P8192730 with the current global settings (exposure −0.6, contrast +40, highlights −70, saturation +15, vibrance +25, graduated filter density 1.5 / hardness 75 / offset 25):
- clarity +15–20 applied only to the sky (luminance mask on bright tones and/or gradient above ~45 %),
- forests, grass, hills and the funicular show **no** added local contrast versus the same render without the masked instance (diff the two exports below the horizon),
- no visible transition line or halo along the horizon at 100 % view.

## Phase 2 — diagnostics (big time saver)

1. **Region preview at 1:1**: `get_image_preview(..., region=(x, y, w, h))` in normalised or pixel coordinates of the cropped output, rendered at full resolution. Used to check halos, crunch and noise before reporting.
2. **Auto-match crop to the camera JPEG**: `match_crop_to_jpeg(raw_path, jpeg_path)` — template-match the user's (possibly cropped) camera JPEG against a RAW render and set the crop. Report the found offset and a match score. (Scale is 1:1 for these Olympus files: JPEG and RAW render share 4640 px width.)
3. **Side-by-side compare**: `compare_images(path_a, path_b, region=?)` returning one image with both, same crop and size.
4. **Export `overwrite` flag**: today export always adds a suffix (`_1`, `_3_1`); allow overwriting a named output.

## Phase 3 — better global tools

1. **Sigmoid (tone mapper) parameters**: contrast, skew, target black/white, and highlight handling if exposed in 5.6.1. Global "punch" through the tone mapper rolls highlights off softly instead of clipping them as color balance rgb contrast does. Note: the server currently never writes a tone mapper — this would add explicit sigmoid params while keeping the workflow default otherwise.
2. **Color equalizer**: per-hue saturation, brightness and hue shift (at least red, orange/yellow, green, cyan, blue, magenta). Use cases: juicier greens like the Olympus JPEG; darker blue sky like a polariser without touching clouds.
3. **Full local contrast params** instead of one `clarity` slider: detail, highlights, shadows, midtone range (and mode, if relevant). Crunchy texture usually comes from the detail on fine structures, not the large edges.

## Phase 4 — nice to have

- **Styles**: apply a saved darktable style at export (`--style`, `--style-overwrite`; requires `--core --configdir` pointing at the darktable config). Lets the user build a base "look" in the GUI and use it as a starting point.
- **Render rotation** (horizon straightening) — currently stored but not rendered.
- **Vignette** — currently stored but not rendered.

## Suggested order

Phase 1 (masks, incl. refinement and mask preview) → Phase 2.1 + 2.2 (1:1 region preview, auto-crop) → Phase 3.1 (sigmoid) → rest as needed.

For each phase: update tool descriptions with the real-world meaning of parameters, add tests on synthetic images, and keep a short note in the README on the XMP structures used and the darktable source they were verified against.
