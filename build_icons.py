# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10.0"]
# ///
"""Render periscope's app icons.

Produces a 1024x1024 master PNG of a dark rounded-square plus a centered
'>' glyph (the prompt arrow that appears throughout periscope's UI), then
shells out to `sips` to downsample for the sizes referenced by the
manifest and the page <link>s.

Re-run when changing the icon design — outputs are committed under
static/. Designed for macOS; SFNSMono / Menlo / Monaco are tried in
order for the glyph.

    uv run build_icons.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
STATIC = ROOT / "static"

MASTER = 1024
RADIUS_PCT = 0.2237  # macOS Big Sur+ app-icon corner ratio.
BG = "#262a32"        # Sits between --bg-0 and --bg-1; reads as its own surface in the dock.
FG = "#f5f7fa"        # Near-white, full contrast against the dark slate.
GLYPH = "❯"      # ❯  — the prompt arrow used throughout periscope.
GLYPH_SCALE = 0.62    # Fraction of canvas height occupied by the glyph cap.

# Order matters: SF Mono looks best when present, Menlo is the reliable
# fallback on every macOS install since Snow Leopard.
FONT_CANDIDATES = (
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.dfont",
)

# Sizes consumed downstream:
#   192  — PWA manifest "any" icon, minimum for installability
#   512  — PWA manifest larger icon (splash, app-launcher contexts)
#    32  — favicon for the browser tab
DOWNSAMPLES = (512, 192, 32)


def _load_font(size_px: int) -> ImageFont.FreeTypeFont:
    # Probe each font with GLYPH and skip any that lack the codepoint —
    # SFNSMono.ttf, for instance, returns a zero-height bbox for '❯'
    # (U+276F, Dingbats block) so PIL would silently render nothing.
    for p in FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(p, size_px)
        except OSError:
            continue
        bbox = f.getbbox(GLYPH)
        if bbox and bbox[3] > bbox[1]:
            return f
    sys.exit(f"no monospace font in {FONT_CANDIDATES} has a glyph for {GLYPH!r}")


def render_master() -> Path:
    img = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [(0, 0), (MASTER, MASTER)],
        radius=int(MASTER * RADIUS_PCT),
        fill=BG,
    )
    font = _load_font(int(MASTER * GLYPH_SCALE))
    bbox = draw.textbbox((0, 0), GLYPH, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # textbbox returns the glyph's actual ink extents; shifting by -bbox[0]/
    # -bbox[1] aligns the ink (not the font's own internal origin) to our
    # centering math.
    x = (MASTER - w) // 2 - bbox[0]
    y = (MASTER - h) // 2 - bbox[1]
    draw.text((x, y), GLYPH, fill=FG, font=font)

    out = STATIC / f"icon-{MASTER}.png"
    img.save(out)
    print(f"wrote {out.relative_to(ROOT)}")
    return out


def downsample(master: Path, size: int) -> None:
    out = STATIC / f"icon-{size}.png"
    subprocess.run(
        ["sips", "-z", str(size), str(size), str(master), "--out", str(out)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(f"wrote {out.relative_to(ROOT)}")


def main() -> None:
    master = render_master()
    for s in DOWNSAMPLES:
        downsample(master, s)


if __name__ == "__main__":
    main()
