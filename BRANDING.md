# Masseberegning — mark and palette

A small identity for a small tool. One mark, one accent colour, and the app's
own UI font. Nothing here needs a designer to change a label.

---

## The mark

An asymmetric terrain profile sitting on a horizontal line. The line is
*ferdig planum* — the finished level — and the shape above it is the mass to be
moved. That is the whole product in one glyph.

Two details are load-bearing, and both came out of testing rather than taste:

**The landform is asymmetric.** A symmetric triangle reads as a generic
mountain, and it makes the mark interchangeable with every outdoor app.

**It sits on the line, not above it.** A symmetric triangle floating above a
horizontal bar is the **eject** icon. The first draft had exactly that, and at
44 px in the sign-in card it read as a media control. Closing the gap and
tilting the peak off-centre fixes it.

### Files

| File | Use |
|---|---|
| `static/brand/mark.svg` | The mark, rounded tile. In-app and anywhere on screen. |
| `static/brand/mark-square.svg` | Full-bleed square, no rounding. Source for app icons. |
| `static/brand/mark-mono.svg` | Single colour via `currentColor`. Print, watermarks, dark grounds. |
| `static/favicon.svg` | Same as `mark.svg`. Modern browsers prefer it. |
| `static/favicon.ico` | 16/24/32/48 px. Older browsers and Windows pinned sites. |
| `static/favicon-16/32/48.png` | Explicit raster fallbacks. |
| `static/apple-touch-icon.png` | 180 px, no rounding — iOS applies its own mask. |
| `static/icon-192.png`, `icon-512.png` | Referenced by `site.webmanifest`. |

`mark-square.svg` exists because iOS and Android round icons themselves. Feed
them a pre-rounded icon and it gets rounded twice, which makes it look inset
and slightly wrong in a way that is hard to place.

### Sizes it has to survive

The mark was drawn at 64 px and checked at 88, 44, 32, 24 and 16 px on both
light and dark grounds. **16 px is the real constraint** — it is the browser
tab, and a mark that dissolves there is not a favicon, however good it looks on
a slide. At 16 px the landform still occupies about 14 % of the tile, which is
enough to read as a shape rather than a smudge.

If you redraw it, redo that test. Three of the five first drafts failed it, and
none of the failures were visible at 96 px.

### Clear space and minimum size

- Clear space: one quarter of the mark's width on every side.
- Minimum on screen: 16 px. Below that, use the wordmark alone.
- Do not add a drop shadow, gradient, or outline. The tile is the container.
- Do not recolour the landform. Teal tile, white landform; or `mark-mono`.
- Do not stretch it. The tile is square and the geometry assumes it.

---

## The wordmark

There isn't an SVG one, deliberately.

The name is set in the app's own UI font — `system-ui` — through the `.brand`
class in `auth.css`. An SVG wordmark would either embed a font file or freeze
the letterforms to whichever font happened to be installed when it was drawn,
and then sit slightly out of step with every other piece of type in the
interface. Setting it in HTML means the lockup always matches the UI.

```html
<div class="brand">
  <img class="brand-mark" src="brand/mark.svg" width="32" height="32" alt="">
  <h1>Masseberegning</h1>
</div>
```

`alt=""` is correct here: the adjacent `<h1>` already carries the name, so
giving the image alt text would make a screen reader announce it twice.

For a slide or a document where you cannot use HTML, place `mark.svg` beside
the name set in the deck's own sans-serif at a similar weight. It is a
two-element lockup, not a locked logotype.

---

## Palette

The accent was already in the app — the drawn polygon and the primary buttons.
The cut/fill pair comes from `render.py`'s diverging ramp, so the interface and
the map overlay speak the same language.

| Role | Hex | Where |
|---|---|---|
| Accent (teal) | `#0f766e` | Mark tile, primary buttons, polygon, links |
| Accent, pressed | `#115e59` | Hover and active states |
| Accent tint | `#e6f2f0` | Badge fills, quiet highlights |
| Ink | `#1a1d21` | Body text and headings |
| Muted ink | `#5b6570` | Secondary text, table headers |
| Hairline | `#e2e6ea` | Borders and rules |
| Ground | `#f6f7f8` | Page background behind cards |
| **Skjæring** (cut) | `#d73027` | Overlay ramp, result emphasis |
| **Fylling** (fill) | `#2171b5` | Overlay ramp, result emphasis |

Cut-red and fill-blue are **data colours, not brand colours.** They mean a
specific thing on the map, so using them for a button or a heading would
quietly break that association. The mark stays teal and white for the same
reason: an early draft used the cut/fill colours in the logo, and besides
dying below 40 px, it spent meaning that the overlay needs.

`theme_color` in `site.webmanifest` and the `<meta name="theme-color">` tag are
both the accent teal, so the browser chrome matches on mobile.

---

## One thing to watch in the repo

`.gitignore` blanket-ignores `*.png` — render.py writes PNGs and `test_ui.py`
leaves a screenshot behind. The brand icons are only committed because of the
explicit negations:

```
!static/*.png
!static/brand/*.png
!static/vendor/images/*.png
```

Add a PNG anywhere else under `static/` and it will be silently ignored, and
the published site will 404 on it. That failure mode is quiet — the site just
loses its icon — so check `git status` after adding one.
