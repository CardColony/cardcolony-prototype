# CardColony — Brand Guide

The identity is one mark, two colors, and no third meaning. This directory holds the
finished assets (`brand/`) plus this written spec and a visual spec page (`brand/preview.html`).

> **中文要点**：标志＝C 形小机器人（C 笔画＋空腔双目＋天线）；严格双色——
> **线色**（C 笔画／天线杆／`ard`）与**点色**（双目／天线珠／`Colony`）三处点色永远同色；
> 蜂蜜金**不借**产品 UI 的语义色，在应用里只表示"这是品牌"，不表示任何状态。

*中文摘要见文末 · Chinese summary below.*

## The mark

A C-shaped agent: the letter **C** with a visor band — two eyes set in its hollow, an
antenna on top. One stroke, two readings: the first letter of *CardColony*, and a small agent.
In the wordmark the symbol **replaces the first C** — `ardColony` follows with **zero gap**.

Master grid **240 × 240** (all assets share this geometry):

| Property | Value |
|---|---|
| C stroke | `26` u, round cap and join |
| Eye radius / spacing | `11` u / centres `40` apart at (102,112) and (142,112) |
| Antenna stem | `6` u, y46 → y30 |
| Antenna bead | `r 7` u at (120,24) |
| C body vertical span | y48 → y192 (antenna rises above cap height like a diacritic) |
| Bold variant (`-sm`) | stroke `30`, eyes `r 14`, bead `r 9` — same grid |

**Clearspace** = one eye diameter (`22` u ≈ 9.2 % of mark width) on all four sides.
**Minimum size**: mark 16 px digital / 5 mm print; wordmark 90 px wide digital. Below 48 px
always switch to the `-sm` weight.

## Two-color rule

Strictly two colors per lockup, never more:

- **Line color** — the C stroke, the antenna stem, and `ard`
- **Point color** — the eyes, the antenna bead, and `Colony`

The three point-colored elements always match. Nothing else inside the mark may use the point color.

## Color tokens

| State | Context | Line color | Point color | UI token |
|---|---|---|---|---|
| **Dark** (primary) | the product UI, dark surfaces | `#e6edf3` | `#e2a63d` honey gold | `--brand-line` · `--brand-gold` |
| **Light** | white docs, README, print | `#1c2733` | `#c9901f` deep honey | same tokens, light theme |
| **Mono** | single-color print, engraving | `#1c2733` | = line color | — |

### Contrast (WCAG 2.x relative luminance; logo-exempt, given for placement decisions)

| Pair | Ratio |
|---|---|
| line `#e6edf3` on UI bg `#0c1015` | **16.15 : 1** |
| point `#e2a63d` on UI bg `#0c1015` | **8.86 : 1** |
| line `#e6edf3` on UI surface `#12171e` | **15.23 : 1** |
| point `#e2a63d` on UI surface `#12171e` | **8.36 : 1** |
| line `#1c2733` on white | **15.14 : 1** |
| point `#c9901f` on white | **2.80 : 1** — see below |

Logotypes are exempt from WCAG 1.4.3 / 1.4.11, so none of these block a deployment. The one
number worth remembering is the last: the light-state gold alone is under 3:1 on pure white.
The light lockup works because the **C stroke** carries the shape; never place bare gold on
white as the only element at small size, and never re-color the eyes to fix it.

The honey gold is deliberately **not** any of the product UI's semantic colors — warning amber,
alive green `#35d0a0`, primary blue `#4d9fff` and the state set (`--st-run` … `--st-arch`) keep
their meanings inside the app. The brand never borrows them, and gold never appears on buttons,
badges or state indicators: if a user can read information out of the color, it is not this gold.

## Variants

| File | Use |
|---|---|
| `cardcolony-mark-{dark,light}.svg` | standard mark, ≥ 48 px |
| `cardcolony-mark-{dark,light}-sm.svg` | bold-weight mark, ≤ 48 px (thicker C, larger eyes) |
| `cardcolony-mark-mono.svg` | single-color contexts |
| `cardcolony-wordmark-{dark,light}.svg` | symbol-as-C lockup (text is live `<text>` — outline before mass distribution) |
| `favicon-{16,32,48}.png` | white rounded tile — holds on light and dark browser chrome |
| `appicon-{180,512}.png` | white rounded-square app icon |
| `og-image.png` | 1200 × 630 social preview card |

## Wordmark lockup

- Lockup viewBox `0 0 716 196`; mark occupies x16→121, text starts at x125 — **zero gap**
- Text: baseline y140, size `120`, weight `650`, letter-spacing `1`
- `<tspan>ard</tspan>` = line color, `<tspan>Colony</tspan>` = point color
- Typeface: Segoe UI Semibold (fallback `system-ui`, weight 650)
- Below 32 px text use the `-sm` mark; below 90 px lockup width the wordmark is out of spec

## App icons & social

Both are a **white rounded tile** so the mark reads identically on light and dark browser chrome
and in store listings. The `og-image.png` places the dark wordmark on the dark ground with a
one-line positioning statement.

## In the product UI

- Top bar / header: dark wordmark at 19–20 px text, 44 px bar
- Nav tree, card chrome, loading and empty states: mark only, dark state
- Assemble the wordmark lockup from the `-sm` mark at `1em` height (baseline nudge `.045em`) —
  do not stretch the mark's own SVG to text height
- Gold inside the app means "this is the brand", never a status

## Don'ts

- Don't color the eyes, bead or `Colony` with UI semantic colors — the point color is brand-owned
- Don't put a gap between the mark and `ard`, and don't build icon + full name as a lockup
  (choose one; the symbol-as-C is primary). When the mark stands alone the full name is
  `CardColony` — do not write `ardColony` after a standalone mark
- Don't outline, shadow, gradient, box or rotate the mark; don't crop or shorten the antenna
- Don't use the light state on dark grounds or the dark state on light
- Don't place the point color on amber-tinted fills
- Don't scale non-uniformly or re-stroke the mark by hand — only the `-sm` variant differs
- Don't ship the standard weight below 48 px, or anything below 16 px

## Files in this directory

```
cardcolony-mark-{dark,light}.svg          standard mark, ≥48 px
cardcolony-mark-{dark,light}-sm.svg       bold mark, ≤48 px
cardcolony-mark-mono.svg                  single color
cardcolony-wordmark-{dark,light}.svg      symbol-as-C lockup (outline text for mass use)
favicon-{16,32,48}.png  appicon-{180,512}.png  og-image.png
README.md                                 this spec
preview.html                              visual spec page (open locally; not shipped in-app)
```

## Known deltas and open items

Stated rather than smoothed over:

1. **Line color by 2/255.** The SVGs use `#e6edf3`; the product UI's `--brand-line` token is
   `#e8edf3` (taken from `--text`). Visually identical, numerically not. Recomposing the SVGs to
   `#e8edf3` would make the asset and the token literally the same value — not done here because
   the asset set is signed off as produced.
2. **Wordmark text is live text, not outlines.** Fine for UI and docs; must be converted to paths
   before any vector distribution (print vendor, third party). Font substitution changes the
   lockup width and breaks the zero-gap relationship.
3. **PNG pixel dimensions** (favicon 16/32/48, appicon 180/512, og 1200×630) are as produced with
   the icon set; they were not re-measured from the binary files while writing this spec.
4. **Print equivalents** — no CMYK or Pantone match is specified yet. Mono covers single-color
   print; for four-color work a press proof is needed before the gold is committed.
5. **Clearspace** is stated as one eye diameter; no dedicated clearspace asset exists, so it must
   be applied by hand in layouts.

## Chinese summary · 中文摘要

- **标志构成**：240×240 网格，C 笔画宽 26（圆头圆角）、双目半径 11/间距 40、天线杆 6、天线珠 r7。
  ≤48px 换 `-sm`（笔画 30、眼 r14、珠 r9）。
- **双色规则**：线色＝C 笔画＋天线杆＋`ard`；点色＝双目＋天线珠＋`Colony`；点色三处永远同色。
- **色值**：暗态 `#e6edf3` / `#e2a63d`；亮态 `#1c2733` / `#c9901f`；单色 `#1c2733`。
  暗态线色对 UI 底 16.15:1、点色 8.86:1；亮态线色对白 15.14:1，**点色对白 2.80:1**（标志豁免
  WCAG，但小尺寸不得只靠金色）。
- **留白与尺寸**：留白＝一个眼直径（22u≈9.2%）；标志最小 16px、字标最小宽 90px；
  ≤48px 必用 `-sm`，字标文字 ≤32px 必用 `-sm`。
- **字标**：符号即首字母 C，`ard` 与符号零间距；Segoe UI Semibold 650；量产前须转曲。
- **禁忌**：点色不得换成 UI 语义色；符号与名称不得并排；不得描边/投影/渐变/旋转/裁天线；
  深浅态不得互用于反向底色。
- **应用内**：顶栏 44px 用暗态字标 19–20px；其余位置只用标志；金色在应用里只代表品牌，绝不承载状态。
