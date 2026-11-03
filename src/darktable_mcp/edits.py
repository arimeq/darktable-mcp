from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import json
from typing import Optional


@dataclass
class CropState:
    left: float = 0.0    # normalised 0.0–1.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0
    rotation: float = 0.0  # degrees, clockwise


@dataclass
class AdjustmentState:
    # --- Exposure ---
    exposure_ev: float = 0.0          # stops, -5 to +5
    black_level: float = 0.0          # 0.0–0.5
    highlight_recovery: float = 0.0   # 0.0–1.0
    shadow_lift: float = 0.0          # 0.0–1.0

    # --- White balance ---
    temperature_kelvin: Optional[float] = None  # None = use camera WB
    tint: float = 0.0                           # -100 to +100 (green↔magenta)

    # --- Tone ---
    contrast: float = 0.0     # -100 to +100
    brightness: float = 0.0   # -100 to +100
    highlights: float = 0.0   # -100 to +100
    shadows: float = 0.0      # -100 to +100
    whites: float = 0.0       # -100 to +100
    blacks: float = 0.0       # -100 to +100

    # --- Colour ---
    saturation: float = 0.0   # -100 to +100
    vibrance: float = 0.0     # -100 to +100

    # --- Detail ---
    sharpness: float = 0.0      # 0 to 100
    noise_reduction: float = 0.0  # 0 to 100

    # --- Effects ---
    vignette: float = 0.0   # -100 to +100 (negative = dark edges)
    clarity: float = 0.0    # -100 to +100 (local contrast)
    dehaze_strength: float = 0.0  # darktable haze removal: -1 to +1 (negative adds haze)
    dehaze_distance: float = 0.2  # 0 = foreground only, 1 = whole scene


@dataclass
class EditState:
    source_path: Path
    adjustments: AdjustmentState = field(default_factory=AdjustmentState)
    crop: Optional[CropState] = None
    output_name: Optional[str] = None  # stem only, no extension

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _sidecar(image_path: Path) -> Path:
        return image_path.with_name(image_path.stem + ".mcp.json")

    @classmethod
    def load(cls, image_path: Path) -> Optional[EditState]:
        sidecar = cls._sidecar(image_path)
        if not sidecar.exists():
            return None
        with open(sidecar) as f:
            data = json.load(f)
        state = cls(source_path=image_path)
        adj_data = data.get("adjustments", {})
        # Only set fields that exist in the dataclass
        valid = {k: v for k, v in adj_data.items() if hasattr(state.adjustments, k)}
        state.adjustments = AdjustmentState(**valid)
        crop_data = data.get("crop")
        state.crop = CropState(**crop_data) if crop_data else None
        state.output_name = data.get("output_name")
        return state

    def save(self):
        sidecar = self._sidecar(self.source_path)
        data = {
            "adjustments": asdict(self.adjustments),
            "crop": asdict(self.crop) if self.crop else None,
            "output_name": self.output_name,
        }
        with open(sidecar, "w") as f:
            json.dump(data, f, indent=2)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def update(self, params: dict):
        for key, value in params.items():
            if hasattr(self.adjustments, key):
                setattr(self.adjustments, key, value)

    def has_changes(self) -> bool:
        defaults = AdjustmentState()
        a = self.adjustments
        return any([
            a.exposure_ev != defaults.exposure_ev,
            a.contrast != defaults.contrast,
            a.saturation != defaults.saturation,
            a.temperature_kelvin is not None,
            a.sharpness != defaults.sharpness,
            a.noise_reduction != defaults.noise_reduction,
            a.vignette != defaults.vignette,
            a.dehaze_strength != defaults.dehaze_strength,
            self.crop is not None,
            self.output_name is not None,
        ])

    def to_dict(self) -> dict:
        return {
            "adjustments": asdict(self.adjustments),
            "crop": asdict(self.crop) if self.crop else None,
            "output_name": self.output_name,
        }
