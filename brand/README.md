# CardColony Brand Guide

The mark is a **C-shaped agent**: the letter C with a visor-band face — two eyes
in its hollow, an antenna on top. One stroke, two readings: the first letter of
*CardColony*, and a little agent. In the wordmark the symbol **replaces the
first C** — `ardColony` sits directly after it, no gap.

> 中文要点：符号＝C 形小机器人（空腔双目＋天线）；字标里它顶替首字母 C，
> 后面直接接 `ardColony`。双目/天线珠/"Colony" 三处永远同一个点色。

## Two-color rule

The identity is a strict **two-color system**:

- **Line color** — the C stroke, the antenna stem, and `ard`
- **Point color** — the eyes, the antenna bead, and `Colony`

Three point-colored elements always match. Nothing else may use the point color
inside the mark.

## Color tokens

| State | Context | Line color | Point color |
|---|---|---|---|
| **Dark (primary)** | the product UI, dark surfaces | `#e6edf3` | `#e2a63d` honey gold |
| **Light** | white docs, README, print | `#1c2733` | `#c9901f` deep honey |
| **Mono** | single-color print, engraving | `#1c2733` | = line color |

The honey gold is deliberately **not** any of the product UI's semantic colors —
warning amber `#f7c163`, alive green `#7fd6a1`, primary blue `#8fb0ff` keep their
meanings inside the app; the brand never borrows them. The gold is adjacent to
the UI's warm family but has its own equity, so the logo is recognizable without
being misread as a status signal.

## Variants

| File | Use |
|---|---|
| `cardcolony-mark-{dark,light}.svg` | standard mark, ≥48 px |
| `cardcolony-mark-{dark,light}-sm.svg` | bold-weight mark, ≤48 px (thicker C, larger eyes) |
| `cardcolony-mark-mono.svg` | single-color contexts |
| `cardcolony-wordmark-{dark,light}.svg` | symbol-as-C lockup (text is Segoe UI Semibold — outline before mass distribution) |
| `favicon-{16,32,48}.png` | white rounded tile — readable on light and dark browser chrome |
| `appicon-{180,512}.png` | white rounded-square app icon |
| `og-image.png` | 1200×630 social preview card |

## Wordmark lockup

- Symbol replaces the first `C`; `ardColony` follows with **zero gap**
- C-body sits on the text baseline; the antenna extends above cap height like a diacritic
- `ard` = line color, `Colony` = point color
- Typeface: Segoe UI Semibold (fallback `system-ui`, weight 650)
- For small lockups (≤32 px text) use the `-sm` mark

## Clearspace & minimum size

- Clearspace = the eye diameter (≈ 11 % of mark width) on all sides
- Min size: mark 16 px digital / 5 mm print; wordmark 90 px wide digital
- Below 48 px always switch to the `-sm` weight

## Don'ts

- Don't color the eyes with UI semantic colors (amber/green/blue) — the point color is brand-owned
- Don't put a gap between the mark and `ard`, don't write `CardColony` next to the mark as icon+text (choose one of the two lockups; the symbol-as-C is the primary)
- Don't outline, shadow, gradient, or rotate the mark; don't crop the antenna
- Don't use the light state on dark backgrounds or the dark state on light
- Don't place the point color on amber-tinted backgrounds

## In the product UI

- Top bar / header: dark-state wordmark, ≈19–20 px text
- Nav tree, card chrome, loading states: mark only (dark state)
- The honey gold inside the app means "this is the brand", never a status — keep it off buttons, badges, and state indicators
