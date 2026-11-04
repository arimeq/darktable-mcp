# DarktableMCP

<p align="center">
  <img src="icon.png" alt="DarktableMCP Logo" width="160"/>
</p>

> Developed by [Yadullah Abidi (YaddyVirus)](https://github.com/YaddyVirus)

A Model Context Protocol (MCP) server that lets you edit photos using Claude as your AI photo editor. Works with **Claude Desktop** and **Claude Code**.

Tell Claude what you want — *"make this warmer and more dramatic"*, *"crop to 16:9"*, *"recover the blown highlights"* — and it applies the edits, shows you a preview, and exports the final image. Supports RAW files (CR2, NEF, ARW, etc.) and JPEGs.

## Features

- 🖼️ **RAW file support** — CR2, NEF, ARW, RAF, DNG, ORF, RW2, and more
- 🎨 **Full editing toolkit** — exposure, white balance, contrast, highlights/shadows, saturation, vibrance, clarity, haze removal, sharpness, noise reduction, vignette
- ✂️ **Crop & rotate** — free crop, aspect ratio crop (16:9, 4:3, 1:1…), straighten
- 📝 **Rename outputs** — give your exports meaningful names
- 📊 **Histogram analysis** — Claude checks clipping and tonal distribution before suggesting edits
- 💾 **Non-destructive** — all edits stored in a sidecar JSON; originals never touched
- 🔄 **Darktable XMP export** — edits written as `.xmp` sidecars Darktable can read
- 📋 **Batch copy settings** — apply one image's edits to many others

---

## Requirements

- Python 3.10 or newer
- [Claude Desktop](https://claude.ai/download) (free or Pro)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/YaddyVirus/darktable-mcp
cd darktable-mcp
```

### 2. Install the package

```bash
pip install -e .
```

This installs all dependencies automatically:
- `mcp` — Model Context Protocol SDK
- `rawpy` — RAW file decoding
- `Pillow` — image processing
- `numpy` — array operations
- `piexif` — EXIF metadata

### 3. Verify it works

```bash
python -m darktable_mcp
```

You should see it start (it will wait for MCP input — press Ctrl+C to exit). If it starts without errors, you're good.

---

## Connecting to Claude Desktop

### 1. Find your Claude Desktop config file

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS    | `~/Library/Application Support/Claude/claude_desktop_config.json` |

### 2. Add the server

Open the config file and add the `mcpServers` block. If the file already has content, merge carefully — don't replace the whole file.

**Windows:**
```json
{
  "mcpServers": {
    "darktable": {
      "command": "python",
      "args": ["-m", "darktable_mcp"]
    }
  }
}
```

**macOS / Linux:**
```json
{
  "mcpServers": {
    "darktable": {
      "command": "python3",
      "args": ["-m", "darktable_mcp"]
    }
  }
}
```

> **Tip:** If `python` isn't on your PATH, use the full path to the Python executable.  
> Windows example: `"C:\\Users\\YourName\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"`  
> Find it by running `where python` (Windows) or `which python3` (macOS/Linux) in a terminal.

### 3. Restart Claude Desktop

Fully quit (don't just close the window) and reopen. The **darktable** server should appear when you select Connectors in Claude Chat or Code.

---

## Usage

Once connected, just talk to Claude naturally:

```
My photos are in /Users/me/Pictures/Trip

Make IMG_3500 warmer and more punchy — it looks flat and cold.

Crop it to 16:9 and export as JPEG at maximum quality, name it "jodhpur_desert".
```

Claude will:
1. Call `list_images` to see your folder
2. Call `get_image_preview` to analyse the photo
3. Call `apply_adjustments` with specific values
4. Look at a fresh preview (pass `save` / `open_viewer` to also write `filename__preview.jpg` and open it)
5. Iterate based on your feedback
6. Export the final image when you're happy

### Available tools

| Tool | What it does |
|------|-------------|
| `list_images` | List all images in a directory |
| `get_image_info` | EXIF metadata + current edit state |
| `get_image_preview` | Render and preview with edits applied; `region` renders one part at full resolution (100 % view) |
| `compare_images` | Two images side by side (e.g. RAW render vs camera JPEG), same region and height |
| `match_crop_to_jpeg` | Crop the RAW to the frame of an edited camera JPEG of the same shot |
| `apply_adjustments` | Exposure, WB, tone, colour, detail, effects |
| `crop_image` | Crop by coordinates or aspect ratio |
| `rotate_image` | Rotate / straighten |
| `reset_crop` | Remove crop, restore full frame |
| `tone_mapper` | darktable sigmoid: contrast, skew, target white/black, hue preservation |
| `color_equalizer` | darktable color equalizer: saturation, brightness, hue shift per hue (e.g. greens, blue sky) |
| `graduated_filter` | Graduated ND filter (darktable graduated density): darken/tint one side, e.g. the sky |
| `add_masked_adjustment` | Adjustments only where a mask selects: drawn gradient and/or luminance, hue, chroma ranges |
| `update_masked_adjustment`, `remove_masked_adjustment`, `list_masked_adjustments` | Manage masked adjustments |
| `export_mask_preview` | Render the masks darktable computes (white = full effect) |
| `rename_output` | Set the export filename |
| `export_image` | Export to JPEG / PNG / TIFF (`overwrite` replaces a file of the same name) |
| `reset_edits` | Undo everything, back to original |
| `get_histogram` | Tonal/clipping analysis |
| `copy_settings` | Copy edits from one image to another |

### Example prompts

- *"What would you suggest to improve this shot?"*
- *"Make it look like a moody film photo"*
- *"Recover the blown sky and open up the shadows"*
- *"Straighten the horizon by about 1.5 degrees"*
- *"Export all photos in this folder with the same settings"*
- *"Reset everything and start fresh"*

---

## How edits are stored

Each image gets a companion `.mcp.json` sidecar file (e.g. `IMG_3500.mcp.json`) that stores all adjustments non-destructively. Your original RAW files are never modified.

On export, a Darktable-compatible `.xmp` sidecar is also written, so you can open the RAW in Darktable and see the edits there too. A sidecar already written by Darktable itself is never overwritten.

When `darktable-cli` is installed, RAW previews, histograms and exports are all rendered by it from that XMP, so what Claude sees is what gets exported. Each result says which renderer was used (`rendered_via`), and falls back to `rawpy` with a warning if darktable-cli fails. If the RAW already has a sidecar written by Darktable:
- if it holds Darktable edits, it is rendered as-is and the tools warn that their edits are not applied;
- if it holds none (Darktable writes one at import), your edits are rendered, but the sidecar is still never overwritten, and the export says the edits were not saved to any sidecar.

| Adjustment | Darktable module |
|---|---|
| exposure, black level | exposure |
| temperature, tint | color calibration (CAT tab illuminant) |
| shadows, highlights | tone equalizer |
| saturation, vibrance | color balance rgb (global chroma, global vibrance) |
| tone mapper | none written unless `tone_mapper` is used: darktable applies the one your workflow uses; `tone_mapper` writes sigmoid (v3), replacing the instance the sigmoid workflow auto-applies |
| contrast | color balance rgb (contrast) |
| clarity | local contrast |
| dehaze | haze removal |
| sharpness, noise reduction | sharpen, denoise (profiled) |
| crop | crop |

Brightness, whites, blacks, vignette, highlight recovery, shadow lift and rotation are not mapped yet: they are stored, but darktable renders ignore them, and the tools say so.

**Masked adjustments.** Each one becomes an extra instance of every module its adjustments map to (e.g. a second
local contrast for "clarity in the sky"), after the global one in the pipe, blended in RGB (scene) through its own
mask, as darktable's "drawn & parametric mask" mode with the parts multiplied. Structures, checked against the
darktable 5.6.1 source and darktable-cli renders:
- blend params: `dt_develop_blend_params_t` v14 (`develop/blend.h`); parametric ranges on the input channels g, Cz and
  hz in the units the GUI shows (`blend_gui.c`), a range that wraps through 0° as the inverted complement;
- gradient: `dt_masks_point_gradient_t` in a group form (`develop/masks.h`, mask version 6), in `masks_history`;
- the extra instances are listed in `iop_order_list` right after their base instance; without it darktable drops them.

darktable stores gradient coordinates in the sensor's frame, before its flip module turns portrait shots upright;
the server maps the rotation and position back by the RAW's orientation, so rotation 0 is always the top of the image
as displayed. Compression keeps darktable's scale: the visible transition spans about 3 × compression % of the frame
diagonal. A luminance range refers to the module's own input: scene-linear before the tone mapper
(exposure, tone equalizer, color balance rgb), display-referred after it (local contrast, sharpen).

**What `exposure_ev = 0` means.** In its scene-referred workflows, Darktable auto-applies an exposure of +0.7 EV to every RAW and adds back any exposure bias set on the camera (a shot at −1 EV gets +1 EV). The server's exposure replaces that instance but builds on its values, so `exposure_ev` is relative to Darktable's default: 0 looks exactly like a freshly imported RAW in Darktable, and +1 is one stop brighter than that.

---

## Troubleshooting

**Server doesn't appear in Claude Desktop**  
Make sure you fully quit and restarted Claude Desktop after editing the config. Check that the JSON is valid (no trailing commas, matching braces).

**`python` command not found**  
Use the full path to your Python executable in the config. Run `where python` (Windows) or `which python3` (macOS) to find it.

**RAW file fails to open**  
Some camera models need an updated version of `rawpy`. Run `pip install --upgrade rawpy`.

**Preview file not opening**  
Previews are only written to disk when you ask for it (`save` or `open_viewer`), as `originalname__preview.jpg` next to your source file. Open it manually if your system doesn't pop it up automatically.

---

## Author

**Yadullah Abidi** — [@YaddyVirus](https://github.com/YaddyVirus)

## License

MIT
