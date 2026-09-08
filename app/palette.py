"""Derive a card theme from album art.

The overlay sits over an unknown desktop and its whole job is to be read, so
this is deliberately conservative: the album only ever supplies a *hue*, never
the lightness that legibility depends on.

Two values come out of an image:

- **plate** — the card background. Always forced very dark. Album art tints it;
  it never brightens it, because white text on a light plate over a light
  desktop is unreadable.
- **accent** — the sung-word glow, progress bar and hairline. Forced bright and
  saturated enough to register against that dark plate.

Naive "dominant colour" extraction fails badly on real covers: the most common
colour in a photo is usually a desaturated skin tone or a near-black background,
which produces a muddy grey theme. The scoring below is modelled on Android's
Palette library -- candidates are ranked by how *vibrant* they are, weighted by
how much of the image they occupy, rather than by population alone.

Greyscale covers are detected and fall back to the neutral default, because
forcing a hue onto a black-and-white sleeve invents colour that is not there.
"""

from __future__ import annotations

import colorsys
import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Analysis size. Album art arrives at 300px; 64px is plenty to find colour
# regions and keeps the whole extraction well under a frame.
SAMPLE_SIZE = 64
# Colours are bucketed to this many levels per channel before counting, so
# near-identical pixels group together instead of splitting the vote.
QUANT_LEVELS = 12

# A swatch scores as `population * saturation ** SATURATION_EXPONENT`.
#
# Both factors are load-bearing. Population alone picks the largest region,
# which on a photographic cover is usually a desaturated background or skin
# tone. Saturation alone picks the single most vivid pixel, which is often a
# stray speck of a logo. Multiplying asks the right question: which colour does
# this cover have *a lot of that is also vivid*.
#
# The exponent was fitted against eleven covers with a known "what colour is
# this" answer. Neighbouring values fail: at 2.5 The Weeknd's red reads yellow,
# and at 4.0 a green cover reads orange. 3.0 is the centre of the stable band.
SATURATION_EXPONENT = 3.0

# Floor for single-pixel noise only (one pixel of the 64x64 sample is 0.00024).
# Kept deliberately low: the population factor in the score already suppresses
# small regions, and a stricter floor discards genuinely small but defining
# elements -- at 0.005 the gold helmets on Random Access Memories are thrown
# away and the cover reads as its blue-grey background.
MIN_POPULATION = 0.001

# Bounds on what can carry a usable hue at all. Near-black and near-white
# pixels have unstable hue, and near-grey pixels have none worth taking.
# These are deliberately wide -- the scoring below decides, not these.
MIN_USABLE_LIGHTNESS = 0.12
MAX_USABLE_LIGHTNESS = 0.90
MIN_USABLE_SATURATION = 0.15

# Below this, treat the cover as monochrome and use the neutral theme.
GREYSCALE_SATURATION = 0.10

# The plate is the card background: tinted, never bright. Matched to the
# neutral plate's own lightness (rgb(12,13,18) is L=0.059) so switching a track
# changes the card's hue without changing how dark it is -- otherwise a themed
# card reads as washed out next to an untinted one.
PLATE_LIGHTNESS = 0.058
PLATE_MAX_SATURATION = 0.42

# The accent has to read against that plate.
ACCENT_MIN_LIGHTNESS = 0.58
ACCENT_MAX_LIGHTNESS = 0.76
ACCENT_MIN_SATURATION = 0.45

NEUTRAL_PLATE = (12, 13, 18)
NEUTRAL_ACCENT = (150, 158, 178)


@dataclass
class Theme:
    plate: Tuple[int, int, int]
    accent: Tuple[int, int, int]
    # False when the art is monochrome and the neutral theme was used, so the
    # UI can skip an animated colour change that would not actually change.
    colorful: bool

    @staticmethod
    def _hex(rgb: Tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    def as_payload(self) -> dict:
        return {
            "plate": self._hex(self.plate),
            "accent": self._hex(self.accent),
            "colorful": self.colorful,
        }


NEUTRAL_THEME = Theme(plate=NEUTRAL_PLATE, accent=NEUTRAL_ACCENT, colorful=False)


def _score(saturation: float, population: float) -> float:
    """Rank a swatch by how much vivid colour it contributes to the cover."""
    return population * (saturation ** SATURATION_EXPONENT)


def _candidates(image) -> List[Tuple[Tuple[float, float, float], float]]:
    """Return [( (h, l, s), population_fraction ), ...] for the quantised image."""
    pixels = list(image.getdata())
    if not pixels:
        return []

    buckets: dict = {}
    step = 256 // QUANT_LEVELS
    for r, g, b in pixels:
        key = (r // step, g // step, b // step)
        entry = buckets.get(key)
        if entry is None:
            buckets[key] = [1, r, g, b]
        else:
            entry[0] += 1
            entry[1] += r
            entry[2] += g
            entry[3] += b

    total = len(pixels)
    out = []
    for count, r_sum, g_sum, b_sum in buckets.values():
        # Average the real pixel values in the bucket rather than using the
        # bucket centre, so the colour stays true to the art.
        r, g, b = r_sum / count, g_sum / count, b_sum / count
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        out.append(((h, l, s), count / total))
    return out


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_rgb(h: float, l: float, s: float) -> Tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (round(r * 255), round(g * 255), round(b * 255))


def theme_from_image(data: bytes) -> Theme:
    """Extract a Theme from raw image bytes. Never raises."""
    try:
        from PIL import Image
    except ImportError:
        return NEUTRAL_THEME

    try:
        image = Image.open(io.BytesIO(data))
        # Flatten transparency onto black; album art is occasionally RGBA and
        # alpha would otherwise skew the averages.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            backdrop = Image.new("RGBA", image.size, (0, 0, 0, 255))
            image = Image.alpha_composite(backdrop, image)
        image = image.convert("RGB").resize((SAMPLE_SIZE, SAMPLE_SIZE))
    except Exception:
        return NEUTRAL_THEME

    candidates = _candidates(image)
    if not candidates:
        return NEUTRAL_THEME

    # A cover with almost no saturated pixels is monochrome. Inventing a hue for
    # it would be worse than leaving it neutral.
    peak_saturation = max((hls[2] for hls, _ in candidates), default=0.0)
    if peak_saturation < GREYSCALE_SATURATION:
        return NEUTRAL_THEME

    usable = [
        (hls, population)
        for hls, population in candidates
        if MIN_USABLE_LIGHTNESS <= hls[1] <= MAX_USABLE_LIGHTNESS
        and hls[2] >= MIN_USABLE_SATURATION
        and population >= MIN_POPULATION
    ]
    if not usable:
        return NEUTRAL_THEME

    hue, lightness, saturation = max(
        usable, key=lambda item: _score(item[0][2], item[1])
    )[0]

    accent = _to_rgb(
        hue,
        _clamp(lightness, ACCENT_MIN_LIGHTNESS, ACCENT_MAX_LIGHTNESS),
        max(saturation, ACCENT_MIN_SATURATION),
    )
    plate = _to_rgb(hue, PLATE_LIGHTNESS, min(saturation, PLATE_MAX_SATURATION))

    return Theme(plate=plate, accent=accent, colorful=True)
