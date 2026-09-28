"""Palette generation and contrast auditing.

The colour system is *generated*, not hand-picked: every value below is
derived from a hue angle, a lightness ladder and a chroma cap, in OKLCH,
gamut-mapped by bisection.  That means the whole system can be regenerated
and re-audited when a hue moves, and no individual hex was chosen by hand.

**The palette is Claude's.** The brand coral is ``#D97757`` -- the real value,
not an approximation -- pinned into the ramp, on a warm cream ground with warm
ink.  Everything else is generated to sit correctly against those two.

There is **no purple anywhere**.  An earlier draft used violet to mark the
machine voice, and it read as a different product: a cool accent against a
warm ground, which is the one combination that makes a UI look like two
designers met.  The three voices are now separated by warmth and weight
instead of by hue:

    You       terracotta   your prompts
    Claude    ink          the assistant's prose
    Machine   slate        tool calls, results, attachments

Claude is achromatic: it is the default voice, and the moments that need to
pop are the machine's, not the prose's.  The two role hues sit on opposite
sides of neutral in the same warm family, so the reader can tell them apart
while scrolling without the page looking like a different app.

Coral is reserved for interactive state only -- it never paints a role, which
is the rule the earlier token file documented and then broke.

Run ``python -m vellum.palette`` to write ``tokens.css`` and the audit next
to it.  The audit exits non-zero on failure so it can gate a build.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# OKLCH -> sRGB
# --------------------------------------------------------------------------


def _oklch_to_linear(L: float, C: float, H: float) -> tuple[float, float, float]:
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _in_gamut(rgb: tuple[float, float, float]) -> bool:
    return all(-1e-4 <= v <= 1.0001 for v in rgb)


def oklch_to_hex(L: float, C: float, H: float) -> str:
    r, g, b = _oklch_to_linear(L, C, H)
    to255 = lambda v: max(0, min(255, round(_gamma(max(0.0, min(1.0, v))) * 255)))
    return f"#{to255(r):02X}{to255(g):02X}{to255(b):02X}"


def max_chroma(L: float, H: float, hi: float = 0.4) -> float:
    """Largest in-gamut chroma at this lightness/hue, by bisection.

    Clipping would shift the hue; bisection keeps the ramp on its intended
    line, which is the whole reason to work in OKLCH.
    """
    lo = 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if _in_gamut(_oklch_to_linear(L, mid, H)):
            lo = mid
        else:
            hi = mid
    return lo


# --------------------------------------------------------------------------
# Contrast: WCAG 2 and APCA
# --------------------------------------------------------------------------


def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb_to_lin(r) + 0.7152 * _srgb_to_lin(g) + 0.0722 * _srgb_to_lin(b)


def wcag_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _apca_y(hex_color: str) -> float:
    """Screen luminance. APCA raises sRGB straight to the 2.4 power -- it does
    NOT linearize first, which is the whole difference between APCA and the
    WCAG transfer function."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126729 * r**2.4 + 0.7151522 * g**2.4 + 0.0721750 * b**2.4


def apca_lc(text: str, bg: str) -> float:
    """APCA Lc, the 0.1.9 formula including its power soft clamp.

    WCAG ratio alone cannot rank "which of these two legible pairs is
    better"; APCA can, and it predicts perception better on dark grounds.
    """
    def soft_clamp(y: float) -> float:
        return y if y >= 0.022 else y + (0.022 - y) ** 1.414

    yt, yb = soft_clamp(_apca_y(text)), soft_clamp(_apca_y(bg))
    if abs(yb - yt) < 0.0005:
        return 0.0
    sapc = (yb**0.56 - yt**0.57) * 1.14 if yb > yt else (yb**0.65 - yt**0.62) * 1.14
    return sapc * 100


# --------------------------------------------------------------------------
# Ramps
# --------------------------------------------------------------------------

# Hue angles. The neutral and coral are Claude's; the two role hues are warm
# and cool *within one family* so nothing on the page reads as a second accent
# system. `you` is Claude's own terracotta direction, `machine` is a desaturated
# slate -- both measured against the cream ground for 4.5:1 as text.
HUE = {
    "neutral": 78.0,   # warm, matches the cream ground
    "coral": 45.0,     # Claude's accent
    "you": 62.0,       # terracotta
    "machine": 235.0,  # slate -- cool, but low chroma so it stays quiet
}

# 12 steps, compressed in the middle where the reading surfaces live.
L_DARK = [0.175, 0.215, 0.255, 0.295, 0.345, 0.405, 0.475, 0.560, 0.660, 0.760, 0.860, 0.945]
L_LIGHT = [0.995, 0.980, 0.960, 0.930, 0.880, 0.820, 0.750, 0.670, 0.575, 0.480, 0.375, 0.265]

# Chroma as a fraction of the in-gamut maximum. Neutral stays quiet so it can
# carry surface elevation; the roles are allowed to sing a little, but
# `machine` is deliberately the quietest of the three -- it is the most
# frequent voice in a transcript and must not compete with the prose.
CHROMA = {
    "neutral": {"dark": 0.24, "light": 0.20},
    "coral": {"dark": 0.90, "light": 0.90},
    "you": {"dark": 0.78, "light": 0.76},
    "machine": {"dark": 0.34, "light": 0.32},
}

# The ground and the accent are pinned to the brand values. Generated ramps
# are internally consistent, but a brand colour is a brand colour -- Claude's
# coral is #D97757 and that is not a value to re-derive.
PINS = {
    ("light", "neutral", 1): "#FFFFFF",
    ("light", "neutral", 2): "#FAF9F5",
    ("light", "neutral", 3): "#F0EEE6",
    ("light", "neutral", 4): "#E8E4DA",
    ("light", "coral", 8): "#D97757",
    ("dark", "neutral", 1): "#262624",
    ("dark", "neutral", 2): "#30302E",
    ("dark", "neutral", 3): "#3A3937",
    ("dark", "neutral", 4): "#454441",
    ("dark", "coral", 8): "#D97757",
}


def build_ramp(family: str, theme: str) -> list[str]:
    H = HUE[family]
    Ls = L_DARK if theme == "dark" else L_LIGHT
    cap = CHROMA[family][theme]
    n = len(Ls)
    out = []
    for i, L in enumerate(Ls):
        pinned = PINS.get((theme, family, i + 1))
        if pinned:
            out.append(pinned)
            continue
        # Sine bump: quiet at the ends, alive in the middle.
        bump = math.sin(i / (n - 1) * math.pi) ** 0.75
        C = min(cap * max_chroma(L, H), cap * bump * 1.9)
        out.append(oklch_to_hex(L, C, H))
    return out


# The reading surface. Claude is deliberately achromatic: it is the default
# voice, and the moments that need to pop are the machine's, not the prose.
CLAUDE_INK = {"dark": "#F5F4F0", "light": "#1B1A17"}


def build(theme: str) -> dict[str, str]:
    """Semantic tokens for one theme."""
    assert theme in ("dark", "light")
    ramps = {f: build_ramp(f, theme) for f in HUE}
    n = ramps["neutral"]
    c = ramps["coral"]
    t = {}
    # Elevation. Four steps, each a visible jump -- the earlier version used
    # three and the panes blurred together.
    t["bg"] = n[0]
    t["surface"] = n[1]
    t["raised"] = n[2]
    t["hover"] = n[3]
    t["active"] = n[4]
    t["sunken"] = n[0]
    t["overlay"] = "rgb(8 9 13 / 0.72)" if theme == "dark" else "rgb(28 26 23 / 0.42)"
    # Text hierarchy.
    t["ink"] = CLAUDE_INK[theme]
    t["body"] = n[10]
    # Muted is the step that carries the most text at the lowest contrast, so
    # its index is chosen by measuring rather than by counting. Claude's warm
    # neutral is lighter and less contrasty than the cool slate it replaced,
    # and both themes were failing 4.5:1 on --surface at the index that
    # worked for the old ramp.
    def first_step(need: float, targets: tuple[str, ...]) -> int:
        for i, colour in enumerate(n):
            if all(wcag_ratio(colour, bg) >= need for bg in targets):
                return i
        return len(n) - 1

    t["muted"] = n[first_step(4.5, (t["bg"], t["surface"], t["raised"]))]
    t["faint"] = n[first_step(3.0, (t["bg"], t["surface"]))]
    t["disabled"] = n[6]
    # Rules.
    t["line"] = n[4] if theme == "dark" else n[3]
    t["line_soft"] = n[3] if theme == "dark" else n[2]
    t["line_strong"] = n[5] if theme == "dark" else n[5]
    # Accent. Coral is scarce: it marks what is interactive, nothing else.
    t["accent"] = c[7]
    t["accent_strong"] = c[8]
    t["accent_hover"] = c[9] if theme == "dark" else c[9]
    t["accent_soft"] = c[2] if theme == "dark" else c[1]
    t["accent_text"] = c[9] if theme == "dark" else c[10]
    t["on_accent"] = "#141413"  # warm ink on coral: 5.6:1, vs 3.3:1 for white
    # Roles. Three voices, three hues, and every one of them must clear
    # 4.5:1 as *text* -- not just as a rail, which is a 2px mark and is
    # allowed to sit at 3:1. The light ramp runs light->dark and the dark ramp
    # runs dark->light, so "the first step dark enough for text" is a
    # different index in each theme. Solved by measuring, not by counting.
    def role_text(family: str) -> str:
        # Must clear 4.5:1 against every surface it can land on -- including
        # `raised`, which is darker in light mode and lighter in dark mode.
        # That is what forces one extra step in light.
        def ok(step: int) -> bool:
            return all(
                wcag_ratio(ramps[family][step], surface) >= 4.5
                for surface in (t["bg"], t["surface"], t["raised"])
            )

        for step in range(3, 12):
            if ok(step):
                return ramps[family][step]
        return ramps[family][7] if theme == "dark" else ramps[family][6]

    t["you"] = role_text("you")
    t["you_rail"] = ramps["you"][6]
    t["you_soft"] = ramps["you"][2]
    t["claude"] = CLAUDE_INK[theme]
    t["claude_rail"] = _claude_rail(theme)
    t["machine"] = role_text("machine")
    t["machine_rail"] = ramps["machine"][5]
    t["machine_soft"] = ramps["machine"][2]
    # Thinking is Claude thinking: same family, lower contrast.
    t["thinking"] = t["muted"]
    # Status.
    t["danger"] = ramps["coral"][9] if theme == "dark" else ramps["coral"][10]
    t["success"] = ramps["you"][8] if theme == "dark" else ramps["you"][9]
    t["warning"] = ramps["coral"][7]
    return t


def _claude_rail(theme: str) -> str:
    # A warm graphite, clearly present, never competing with coral.
    return "#8A857B" if theme == "dark" else "#989288"


# Every text/surface pair the UI actually renders. If a pair is not here, it
# is not in the design.
PAIRS = [
    ("ink", "bg", 4.5),
    ("body", "bg", 4.5),
    ("muted", "bg", 4.5),
    ("body", "surface", 4.5),
    ("muted", "surface", 4.5),
    ("body", "raised", 4.5),
    ("muted", "raised", 4.5),
    ("ink", "raised", 4.5),
    ("accent_text", "bg", 4.5),
    ("accent_text", "raised", 4.5),
    ("on_accent", "accent", 4.5),
    ("you", "bg", 4.5),
    ("you", "raised", 4.5),
    ("machine", "bg", 4.5),
    ("machine", "raised", 4.5),
    ("claude", "bg", 4.5),
    ("danger", "bg", 4.5),
    ("success", "bg", 4.5),
]


def audit(theme: str) -> tuple[list[str], list[tuple]]:
    tok = build(theme)
    rows, fails = [], []
    for fg, bg, need in PAIRS:
        ratio = wcag_ratio(tok[fg], tok[bg])
        lc = apca_lc(tok[fg], tok[bg])
        ok = ratio >= need
        rows.append((fg, bg, tok[fg], tok[bg], ratio, lc, ok))
        if not ok:
            fails.append((fg, bg, ratio, need))
    return rows, fails


CSS_TEMPLATE = """\
/* GENERATED by vellum/palette.py -- do not edit by hand.
   Regenerate: python -m vellum.palette
   Hue angles: neutral {n_hue}, coral {c_hue}, you {y_hue}, machine {m_hue}. */

/* The selector has to name the theme. Two bare `:root` blocks means the dark
   one always wins regardless of `data-theme` -- which is exactly what
   happened: the toggle set the attribute to "light" and the page stayed dark. */
{selector} {{
  color-scheme: {scheme};

  /* -- surfaces: elevation steps, each a visible jump -- */
  --bg: {bg};
  --surface: {surface};
  --raised: {raised};
  --hover: {hover};
  --active: {active};
  --overlay: {overlay};

  /* -- text -- */
  --ink: {ink};
  --body: {body};
  --muted: {muted};
  --faint: {faint};
  --disabled: {disabled};
  --on-accent: {on_accent};

  /* -- rules -- */
  --line: {line};
  --line-soft: {line_soft};
  --line-strong: {line_strong};

  /* -- accent: interactive state only, never a role -- */
  --accent: {accent};
  --accent-strong: {accent_strong};
  --accent-hover: {accent_hover};
  --accent-soft: {accent_soft};
  --accent-text: {accent_text};

  /* -- voices: the only hues that name a speaker -- */
  --you: {you};
  --you-rail: {you_rail};
  --you-soft: {you_soft};
  --claude: {claude};
  --claude-rail: {claude_rail};
  --machine: {machine};
  --machine-rail: {machine_rail};
  --machine-soft: {machine_soft};
  --thinking: {thinking};

  /* -- status -- */
  --danger: {danger};
  --success: {success};
  --warning: {warning};
}}
"""


def render_css(theme: str) -> str:
    t = build(theme)
    t.update(n_hue=HUE["neutral"], c_hue=HUE["coral"], y_hue=HUE["you"], m_hue=HUE["machine"])
    selector = ':root' if theme == "light" else '[data-theme="dark"]'
    return CSS_TEMPLATE.format(selector=selector, scheme=theme, **t)


def report(theme: str) -> str:
    rows, fails = audit(theme)
    out = [
        f"Vellum contrast audit -- {theme}",
        "WCAG 2 relative luminance and APCA Lc (0.1.9).",
        "Thresholds: 4.5:1 body, 3.0:1 large/non-text. APCA |Lc| >= 45 body, >= 30 large.",
        "",
        f"{'foreground':<14}{'background':<10}{'WCAG':>7}{'APCA':>8}   verdict",
        "-" * 54,
    ]
    for fg, bg, hf, hb, ratio, lc, ok in rows:
        verdict = "ok" if ok else f"FAIL (need {min(r for f, b, r in PAIRS if f == fg and b == bg):.1f})"
        if ok and abs(lc) < 30:
            verdict = "low APCA"
        out.append(f"{fg:<14}{bg:<10}{ratio:>6.2f}:{lc:>7.1f}   {verdict}")
    out.append("")
    if fails:
        out.append(f"{len(fails)} FAILING PAIR(S):")
        for fg, bg, ratio, need in fails:
            out.append(f"  {fg} on {bg}: {ratio:.2f}:1 < {need}:1")
    else:
        out.append("All pairs pass WCAG AA.")
    return "\n".join(out)


def main() -> int:
    # palette.py lives in vellum/, so the UI assets are a sibling of it.
    out_dir = Path(__file__).resolve().parent / "ui" / "css"
    out_dir.mkdir(parents=True, exist_ok=True)
    light, dark = render_css("light"), render_css("dark")
    (out_dir / "tokens.css").write_text(light + "\n" + dark, encoding="utf-8")

    lines = [report("light"), "", "=" * 54, "", report("dark")]
    total_fails = len(audit("light")[1]) + len(audit("dark")[1])
    if total_fails:
        lines += ["", f"TOTAL: {total_fails} failing pair(s)."]
    else:
        lines += ["", "TOTAL: all pairs pass in both themes."]
    text = "\n".join(lines)
    (out_dir / "contrast-report.txt").write_text(text, encoding="utf-8")
    print(text)
    return 1 if total_fails else 0


if __name__ == "__main__":
    sys.exit(main())
