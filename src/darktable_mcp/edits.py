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
class GraduatedState:
    """darktable graduated density, in the units its GUI shows."""
    density: float = 1.0     # EV, -8 to +8
    hardness: float = 0.0    # %, 0 = soft
    rotation: float = 0.0    # degrees as the GUI slider shows them (the param is its negation)
    offset: float = 50.0     # % position of the transition line (rotation 0: from the top)
    hue: float = 0.0         # degrees
    saturation: float = 0.0  # %, 0 = neutral grey


@dataclass
class SigmoidState:
    """darktable sigmoid (the scene-referred tone mapper), in the units its GUI shows; defaults = darktable's."""
    contrast: float = 1.5           # middle grey contrast, 0.1–10 (soft 0.7–3)
    skew: float = 0.0               # -1 to 1: negative = more room for highlights, positive = for shadows
    white_target: float = 100.0     # %, display white
    black_target: float = 0.0152    # %, display black
    hue_preservation: float = 100.0 # %


HUES = ("red", "orange", "yellow", "green", "cyan", "blue", "lavender", "magenta")


@dataclass
class ColorEqualizerState:
    """darktable color equalizer nodes as its GUI shows them: hue name -> % (saturation, brightness) or ° (hue)."""
    saturation: dict[str, float] = field(default_factory=dict)  # -100 to +100 %
    hue: dict[str, float] = field(default_factory=dict)         # -180 to 180 °
    brightness: dict[str, float] = field(default_factory=dict)  # -100 to +100 %


@dataclass
class GradientMask:
    """darktable drawn gradient mask, placed on the full uncropped frame."""
    rotation: float = 0.0      # degrees, upright frame: 0 = mask on top, 90 = left, 180 = bottom
    offset: float = 50.0       # % position of the transition line, from the edge the mask covers
    compression: float = 50.0  # % of the diagonal, darktable's scale: visible transition ≈ 3×, zero beyond 4×


@dataclass
class MaskState:
    """darktable drawn and/or parametric mask (blend-if, RGB (scene)), in the units its GUI shows."""
    gradient: Optional[GradientMask] = None
    luminance: Optional[list[float]] = None  # "g" (gray) slider, %: [from, full from, full to, to]
    hue: Optional[list[float]] = None        # "hz" slider, degrees; from > to wraps through 0
    chroma: Optional[list[float]] = None     # "Cz" slider as shown (0–1 at default boost)
    invert: bool = False
    feathering_radius: float = 0.0  # px
    feathering_guide: str = "input after blur"
    blur_radius: float = 0.0        # px
    opacity: float = 100.0          # %
    contrast: float = 0.0           # mask contrast, %
    brightness: float = 0.0         # "mask opacity" refinement slider, %

    def __post_init__(self):
        if isinstance(self.gradient, dict):
            self.gradient = GradientMask(**self.gradient)


@dataclass
class MaskedAdjustment:
    id: str
    label: str
    adjustments: dict  # AdjustmentState field -> value
    mask: MaskState

    def __post_init__(self):
        if isinstance(self.mask, dict):
            self.mask = MaskState(**self.mask)


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
    graduated: Optional[GraduatedState] = None
    sigmoid: Optional[SigmoidState] = None
    color_eq: Optional[ColorEqualizerState] = None
    masked: list[MaskedAdjustment] = field(default_factory=list)
    output_name: Optional[str] = None  # stem only, no extension
    # 2: gradient masks are upright (before: in the sensor's frame, which differs on portrait shots)
    version: int = 2

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
        state.graduated = GraduatedState(**data["graduated"]) if data.get("graduated") else None
        state.sigmoid = SigmoidState(**data["sigmoid"]) if data.get("sigmoid") else None
        state.color_eq = ColorEqualizerState(**data["color_eq"]) if data.get("color_eq") else None
        state.masked = [MaskedAdjustment(**m) for m in data.get("masked", [])]
        state.output_name = data.get("output_name")
        state.version = data.get("version", 1)
        return state

    def save(self):
        sidecar = self._sidecar(self.source_path)
        with open(sidecar, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

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
            self.graduated is not None,
            self.sigmoid is not None,
            self.color_eq is not None,
            bool(self.masked),
            self.output_name is not None,
        ])

    def to_dict(self) -> dict:
        return {
            "adjustments": asdict(self.adjustments),
            "crop": asdict(self.crop) if self.crop else None,
            "graduated": asdict(self.graduated) if self.graduated else None,
            "sigmoid": asdict(self.sigmoid) if self.sigmoid else None,
            "color_eq": asdict(self.color_eq) if self.color_eq else None,
            "masked": [asdict(m) for m in self.masked],
            "output_name": self.output_name,
            "version": self.version,
        }
