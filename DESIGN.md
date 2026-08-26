---
name: Plexora
description: A dark, self-contained instrument for reading multiplexed microscopy — one token vocabulary that inverts to paper when the work is headed for print.
colors:
  surface-0: "#05070a"
  surface-1: "#0b0f15"
  surface-2: "#121820"
  border-subtle: "rgba(255, 255, 255, 0.12)"
  border-strong: "rgba(255, 255, 255, 0.22)"
  text-primary: "#f8fafc"
  text-secondary: "#d8e1ed"
  text-muted: "#8ea0b8"
  signal-cyan: "#38bdf8"
  signal-cyan-soft: "rgba(56, 189, 248, 0.36)"
  status-success: "#34d399"
  status-warning: "#f3b845"
  status-danger: "#f87171"
  status-danger-soft: "rgba(248, 113, 113, 0.14)"
  desk-paper: "#f4f5f8"
  desk-card: "#ffffff"
  desk-ink: "#16202e"
  desk-signal: "#2563eb"
  desk-danger: "#d64545"
typography:
  display:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans", "Liberation Sans", Arial, sans-serif'
    fontSize: "26px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "normal"
  headline:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "normal"
  subhead:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "normal"
  title:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0"
  micro:
    fontFamily: "{typography.display.fontFamily}"
    fontSize: "10px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.02em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
rounded:
  sm: "6px"
  md: "8px"
  lg: "12px"
  pill: "999px"
  circle: "50%"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "20px"
  "6": "24px"
components:
  button-primary:
    backgroundColor: "{colors.signal-cyan}"
    textColor: "{colors.surface-0}"
    typography: "{typography.title}"
    rounded: "{rounded.sm}"
    padding: "8px 18px"
  button-primary-hover:
    backgroundColor: "#5cc9fb"
    textColor: "{colors.surface-0}"
  button-primary-disabled:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text-muted}"
  button-secondary:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text-secondary}"
    typography: "{typography.title}"
    rounded: "{rounded.sm}"
    padding: "8px 18px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-2}"
    textColor: "#ffffff"
  input-field:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.text-primary}"
    typography: "{typography.title}"
    rounded: "{rounded.sm}"
    padding: "8px 10px"
  toggle-switch:
    backgroundColor: "rgba(255, 255, 255, 0.16)"
    rounded: "{rounded.pill}"
    width: "28px"
    height: "16px"
  toggle-switch-checked:
    backgroundColor: "{colors.signal-cyan}"
    rounded: "{rounded.pill}"
    width: "28px"
    height: "16px"
  channel-slot:
    backgroundColor: "rgba(8, 21, 30, 0.76)"
    textColor: "{colors.text-primary}"
    rounded: "7px"
    padding: "7px 9px 4px 12px"
  tool-card:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.sm}"
  card-project:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
  menu-item:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    typography: "{typography.title}"
    padding: "4px 12px"
  dialog:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
    padding: "24px"
    width: "min(720px, 92vw)"
---

# Design System: Plexora

## Overview

**Creative North Star: "The Field Station"**

A field station is equipment that has to work wherever it is set down. It brings
everything it needs, depends on no supply line, and is built so that the
instrument never becomes the problem you are solving. Plexora's interface is
built the same way: no webfont, no CDN, no external anything. The whole visual
system is roughly 120 KB of hand-written CSS and one token file, and it renders
identically on a laptop, inside a notebook cell, and through an SSH tunnel from a
compute node where the round trip is 80 ms and the colours are being recompressed
on the way.

The surface is quiet on purpose. Chrome sits at three near-black tones separated
by single-pixel borders; text is muted before it is bright; nothing carries
saturated colour unless it has earned it. What breaks that quiet is always a
state — focused, active, selected, busy, wrong — and because it happens rarely,
it reads instantly when it does. Controls, by contrast, are meant to be felt:
toggles travel, swatches grow under the cursor and compress when pressed, cards
lift two pixels. The system is reserved about colour and generous about feedback.

The one genuinely unusual idea here is that the surround follows the work's
destination. Everywhere the subject is a backlit fluorescence image, the room is
dark. Inside Figure Builder's workspace, where the subject is a figure headed for
a journal page, the exact same token names are rebound to a light palette — white
cards on paper grey, ink-dark text, a deeper blue accent — because judging *is
this grey too pale* against dark chrome puts the eye's adaptation somewhere the
paper is not. Quick Edit, opened inside that light workspace, flips back to dark
for its own subtree, because its subject is the image again. One vocabulary, two
rooms, chosen by what is being looked at.

**Key Characteristics:**

- **Quiet until it matters.** Surfaces are flat and dim at rest; colour, motion
  and edge-strength are reserved for state and mean something every time.
- **Tactile and reassuring.** Controls give feedback you can feel — travel,
  compression, a 2px lift, a 1.06 scale — always small, always immediate.
- **One accent, spent carefully.** Signal Cyan appears 95 times across the whole
  stylesheet corpus. It is the only accent chrome is allowed.
- **Borders, not shadows.** A 1px border and a step in surface tone carry
  structure; shadow is reserved for things that genuinely float.
- **Zero bytes of typography.** The system stack is the typeface. Nothing is
  fetched, nothing is bundled, nothing can fail to arrive.
- **Dense by design.** 11px and 12px are the working sizes. This is a desktop
  instrument with two breakpoints in the entire application.

## Colors

Three near-black surfaces, a three-step text ramp that starts muted, translucent
white borders, and exactly one accent — plus a full light rebinding for the one
place the work is destined for paper.

### Primary

- **Signal Cyan** (`#38bdf8`): The only accent chrome spends. It marks focus
  rings, primary buttons, active tabs and toggles, hovered card borders, and the
  default channel colour. Its hover step is a single brightness increment
  (`#5cc9fb`); it never shifts hue. On a primary button it carries near-black
  text (`#05070a`), never white — the cyan is bright enough that white on it
  fails at 12px.
- **Signal Cyan Soft** (`rgba(56, 189, 248, 0.36)`): The focus atmosphere. Used
  as a 2–3px outer ring on focused controls and as a softened border on a
  selected card, so focus reads as a glow around the control rather than a change
  of the control.

### Secondary

The system has no second chrome accent. Two coloured families exist alongside
Signal Cyan, and both belong to the **data**, not the interface:

- **The channel palette** — ten curated colours the user assigns to fluorescence
  channels: Blue `#2388ff`, Red `#ff2d2d`, Green `#2bd46f`, White `#ffffff`,
  Yellow `#ffd60a`, Magenta `#ec4899`, Cyan `#22e6e6`, Orange `#f97316`, Violet
  `#a78bfa`, Grey `#94a3b8`. These are additively blended into the composite;
  they are pixel values, not UI colours.
- **The tool hues** — eight hues 45° apart, deliberately *not* in wheel order:
  20, 200, 110, 290, 65, 245, 155, 335. Each open tool hashes to a slot and takes
  that hue at `hsl(H 85% 65%)`, with soft (`/0.36`) and quiet (`55% 60% /0.42`)
  derivatives. Interleaving matters: a hash collision walks forward to the next
  slot, and in wheel order those neighbours would be the hardest pair to tell
  apart. Interleaved, every step of the walk moves at least 90°.

### Tertiary

Status colours, used only as status — never as decoration, never as a brand
gesture.

- **Success Green** (`#34d399`): The status indicator at rest; a valid field
  border.
- **Working Amber** (`#f3b845`, soft `rgba(243, 184, 69, 0.14)`): Work in
  flight, and the fill behind a non-blocking warning.
- **Alert Red** (`#f87171`, soft `rgba(248, 113, 113, 0.14)`): Disconnected,
  failed, invalid. Its soft form is the fill behind an error message.

### Neutral

- **Void** (`#05070a`): The page. Also the text colour on a primary button.
- **Panel** (`#0b0f15`): The top bar, cards, dialogs, and the resting state of an
  input field.
- **Inset** (`#121820`): Anything sitting *inside* a panel — dropdown menus, tool
  cards, an input inside a dialog, a disabled button.
- **Hairline** (`rgba(255, 255, 255, 0.12)`): The default border. The single most
  used colour token in the system (98 occurrences) and the main structural
  device.
- **Hairline Strong** (`rgba(255, 255, 255, 0.22)`): A border that is being
  emphasised — a hovered secondary button, a dialog edge, a popover.
- **Paper White** (`#f8fafc`): Primary text and the knob of a toggle switch.
- **Read** (`#d8e1ed`): Secondary text — nav links, body copy in panels.
- **Muted** (`#8ea0b8`): Captions, units, menu icons, keyboard hints, disabled
  text. Used *more* than primary text (102 vs 93 occurrences); the interface
  speaks quietly first.

### The Desk (light rebinding, scoped to `.fb-workspace`)

Not a theme toggle and not user-selectable. The same core token names are
re-pointed for this subtree only, so every `.fb-` rule resolves against paper
without rewriting a single declaration.

- **Desk** (`#f4f5f8`): The workspace ground.
- **Card** (`#ffffff`): A figure panel — the thing being judged.
- **Ink** (`#16202e`) / `#3f4a5c` / `#6b7689`: The three-step text ramp,
  inverted. The muted step is deeper than the dark room's equivalent because it
  carries the whole explanatory layer of the workspace — the note under every
  sidebar section, the units after every number — and the `#78839a` it started
  at was 3.81:1 on a white card.
- **Desk Signal** (`#2563eb`): Signal Cyan on white is a highlighter, so the
  accent goes one step deeper and one step more saturated. It has to survive both
  places this page puts it: chrome on white, and selection handles over a
  fluorescence image.
- **Desk Danger** (`#d64545`): Alert Red on white reads as pink — illegible as
  text, worse as a fill behind white lettering on a delete button.

### Named Rules

**The Destination Rule.** The surround matches where the work is going. Dark
where the subject is a backlit image; light where the subject is ink on paper.
A new surface picks its room by asking what the user is judging, not by
inheriting whatever the last screen used.

**The One Voice Rule.** Signal Cyan is the only accent the interface may spend on
itself. If a new element needs colour, it is either a state (use the status
ramp), an identity that belongs to the data (use a channel colour or a tool hue),
or it does not need colour.

**The Two Palettes Rule.** The chrome palette and the channel palette never
borrow from each other. A UI element must not be tinted with a channel colour to
look lively, and a channel must never be forced to a chrome token to look
on-brand.

## Typography

**Display / Body Font:** the platform UI stack — `system-ui, -apple-system,
"Segoe UI", Roboto, "Helvetica Neue", "Noto Sans", "Liberation Sans", Arial,
sans-serif`
**Mono Font:** `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`

**Character:** Deliberately anonymous. The typeface is whatever the operating
system already renders best, which means it is never the wrong weight on a bad
display, never mid-download, and never absent behind a firewall. Personality is
carried by weight and case — 700-weight uppercase kickers against 400-weight
12px body — not by a face. The mono stack appears only where characters must be
counted or compared: filesystem paths and recorded settings.

### Hierarchy

- **Display** (700, 26px, 1.15): Full-page headings — Open Project, Settings.
  One per page, at the top, and nowhere else.
- **Headline** (700, 20px, 1.1): The project name in the viewer sidebar; major
  section titles. Rendered pure white (`#ffffff`) rather than Paper White where
  it is the sidebar's anchor.
- **Subhead** (600, 15px, 1.25): The one heading a floating surface gets — the
  figure's own name in the Desk topbar, a dialog's question, a nav link. Not a
  page heading and not a control; it is what names the thing you are inside.
- **Title** (600, 13px, 1.3): Form labels, buttons, dropdown items, subtitles.
  The largest size that appears inside a control.
- **Body** (400, 12px, 1.45): Panel content, tool card titles (at 700), status
  text. The working size of the application.
- **Label** (700, 11px, uppercase, letter-spacing 0): Section kickers above a
  group of controls. Muted, never primary — it names the group, it is not the
  group's content.
- **Micro** (600, 10px, 0.02em): Counts, units, row sub-captions, badge text.
- **Mono** (400, 12px): Paths, data directory locations, recorded settings.

### Named Rules

**The Zero-Byte Rule.** No webfont ships and none is fetched. If a design needs a
specific typeface to work, the design is wrong for this product. (Historical
note: `main.css` still declares `'Source Sans Pro'` on `body`, which has never
been loaded — there is no `@font-face`, no font file, and Bootstrap's reboot
overrides it anyway. It is a dead declaration, not an intent.)

**The Eleven-Twelve Rule.** Panel content is 11px or 12px — together these are
137 of the ~250 font-size declarations in the codebase. Anything larger is a
heading and has to justify the room it takes from the image.

**The Tabular Rule.** Any number that changes in place — a coordinate, a
threshold, a cell count, a keyboard hint — is `font-variant-numeric:
tabular-nums`. Digits must not shift the layout as they update.

**The Glyph-Is-Not-Type Rule.** An icon's `font-size` is not a step on the ramp
above. A glyph is the whole picture on the button it sits on and is measured
against that tile; the word under it is type and takes its size from the ramp.
On the Desk the two are named apart — `--fb-icon` (14px) and `--fb-icon-lg`
(15px) — so a glyph can be resized without anybody reading it as a new text
step, and so a literal `font-size` in a stylesheet still means text.

## Layout

**The viewer shell** is a two-column grid: `minmax(320px, 380px)` for the
sidebar, `minmax(0, 1fr)` for the image. The sidebar has a floor and a ceiling
and the image takes everything else, at every window size. The top bar is `5vh`
(capped) and the content area below it is `95vh`; a routed page carries the same
`min-height: 95vh` so the application does not appear to shrink on the way to
Settings.

**Spacing** is a six-step 4px scale — 4, 8, 12, 16, 20, 24. Panel padding is 16px
(`--space-4`); gaps inside a list of controls are 8px; a dialog's form is 24px.
Nothing exceeds 24px: this is a dense instrument, and a 32px gutter is a gutter
taken from the image.

**Page surfaces** (Open Project, Settings, the import form) are centred column
layouts, not full-bleed. The project grid is
`repeat(auto-fill, minmax(200px, 1fr))` with a 16px gap and 4:3 thumbnails.

**Responsive behaviour is minimal and intentional.** The whole application
contains two width breakpoints — 900px and 720px — against seven
`prefers-reduced-motion` queries. There is no tablet layout and no mobile layout,
because there is no scenario where someone reads a 50,000-pixel-square slide on a
phone.

### Named Rules

**The Rail Rule.** The image gets the remainder, always. New controls go into the
sidebar's scroll, into a card over the image, or into a dialog — never into a
third column and never by narrowing the canvas.

**The Desktop Rule.** Design for a desktop window and verify at 720px. Do not
invent a mobile layout for a surface that will never be opened on a phone; do
make sure nothing is unreachable in a half-width window beside a notebook.

## Elevation & Depth

This system is **border-first**. Depth is a 1px translucent-white border plus a
step in surface tone: Void → Panel → Inset. The border tokens are used 130 times
between them; all three shadow tokens together are used 13 times. A surface that
merely sits on another surface gets a border and a tone step, and no shadow at
all.

Shadow is reserved for things that genuinely float above the page and would
otherwise be ambiguous about which layer they are on — dropdown menus, popovers,
dialogs — and for one structural case: the viewer sidebar casts `12px 0 28px
rgba(0, 0, 0, 0.35)` rightward onto the image, which is what makes the panel read
as being in front of the specimen rather than beside it.

The sidebar is additionally the only surface with a gradient: a vertical
`rgba(20, 24, 31, 0.98)` → `rgba(10, 13, 18, 0.98)`, so the panel darkens as it
descends and does not read as one flat slab at 380px wide.

### Shadow Vocabulary

- **`--shadow-sm`** (`0 2px 8px rgba(0, 0, 0, 0.25)`): Small floating chrome.
- **`--shadow-md`** (`0 10px 28px rgba(0, 0, 0, 0.35)`): Dropdown menus,
  popovers, the colour-swatch picker. The default for anything transient.
- **`--shadow-lg`** (`0 12px 28px rgba(0, 0, 0, 0.4)`): Modal dialogs, over a
  `rgba(0, 0, 0, 0.62)` backdrop.
- **Sidebar cast** (`12px 0 28px rgba(0, 0, 0, 0.35)`): Structural, horizontal,
  the panel over the image.
- **Desk float** (`0 1px 2px rgba(15, 23, 42, 0.06), 0 8px 28px rgba(15, 23, 42,
  0.12)`): Figure Builder only. Two shadows, not one — the tight pair is what
  makes a card read as lifted a few millimetres rather than pasted on.

### Named Rules

**The Border-First Rule.** Reach for a border and a tone step before reaching for
a shadow. A shadow is a claim that the element is on a different layer; if that
is not true, it is noise.

**The Focus-Is-Atmosphere Rule.** Focus is a 2–3px `signal-cyan-soft` ring
outside the control plus a Signal Cyan border on it — never a change of the
control's fill, and never a removed outline with nothing in its place.

## Shapes

Corners are small and consistent. `--radius-sm` (6px) is the default and appears
51 times — more than the other two radius tokens combined. 8px is for a surface
that contains other surfaces (dropdown menu, popover, an alert block). 12px is
for whole objects: project cards, dialogs. Full pills (999px) exist only on
toggle tracks, and circles (50%) only on the status glyph, avatars, and the
mini-map lens.

Borders are always 1px and always translucent white — never a solid grey, which
would go muddy against three different near-black surfaces.

The system's recurring silhouette is the **identity edge**: an object that
carries a colour of its own wears it as a 3px vertical bar down its left side,
not as a fill or a full border. A channel slot draws it as a `::before`
pseudo-element in the channel's assigned colour; a tool card draws it as `inset
3px 0 0` in the tool's hue. Both are quiet at rest and go to full strength when
active. This is what lets eight coloured objects sit in one 380px column without
the column becoming a paint chart.

### Named Rules

**The Six-Pixel Default Rule.** New chrome gets 6px. Go to 8px only when the
element contains other rounded elements, and to 12px only when the element is a
whole card or dialog. Never invent a fourth value — the codebase already carries
2px, 3px, 5px, 7px, 9px, 10px and 14px one-offs, and each one is a small failure
to fix, not a precedent to follow.

**The Three-Pixel Edge Rule.** Colour that identifies an object goes on its left
edge at 3px, quiet at rest and full-strength when active. It never becomes a
background fill, because a filled card in eight hues is a toy and the sidebar has
to hold eight of them at once.

## Components

Controls are **tactile and reassuring**. Feedback is immediate, physical, and
small: a toggle knob travels 12px, a swatch scales to 1.06 on hover and 0.94 on
press, a card lifts 2px. Everything transitions on `--duration-fast` (120ms) or
`--duration-base` (180ms) with `cubic-bezier(0.2, 0, 0, 1)`, and both collapse to
`0ms` under `prefers-reduced-motion`.

### Buttons

- **Shape:** Gently rounded (6px, `--radius-sm`), 13px/600, `8px 18px` padding.
- **Primary:** Signal Cyan fill with near-black text (`#05070a`), no border.
  Hover brightens to `#5cc9fb`; the text colour does not change. Disabled drops
  to an Inset fill with Muted text.
- **Secondary:** Inset fill, Hairline border, Read text. Hover keeps the fill and
  strengthens the border to Hairline Strong while the text goes pure white — the
  button gets crisper, not lighter.
- **Icon button:** Transparent, Muted glyph, no border at rest; hover brings the
  glyph to primary and adds a faint fill. Used in card headers and toolbars where
  a labelled button would not fit.

**Note on the current state:** there is no single button primitive. The codebase
carries `.btn-primary` overrides scoped to `.import-page`, plus `.fb-button`,
`.icon-button`, `.browse-button`, `.view-toggle-button`, `.list-button` and
others. `.fb-button` had drifted to a 9px radius and a 34px fixed height and is
now back on 6px and `--fb-control-h`. New work should follow the
primary/secondary specification above and consolidate toward it rather than
adding a ninth variant.

### Inputs / Fields

- **Style:** Panel fill (Inset when inside a dialog), 1px Hairline border, 6px
  radius, 13px text, `8px 10px` padding. Placeholder in Muted.
- **Focus:** Border goes Signal Cyan, plus a `0 0 0 3px signal-cyan-soft` ring
  and `outline: none`. The fill does not change.
- **Valid / Invalid:** Border only — Success Green or Alert Red. No icon, no
  fill, no layout shift.
- **Readonly / Disabled:** Text drops to Muted and the cursor becomes
  `not-allowed`. The field is still readable; it is not greyed out of legibility.

### Cards / Containers

- **Corner style:** 12px (`--radius-lg`) for a whole card.
- **Background:** Panel (`#0b0f15`), 1px Hairline border, no shadow at rest.
- **Hover:** Border goes Signal Cyan and the card lifts `translateY(-2px)` over
  180ms. Both properties transition; nothing else moves.
- **Internal padding:** 12px, with the thumbnail bleeding to the card edge
  (`overflow: hidden` on the card, 4:3 aspect ratio on the image).

### Navigation

- **Top bar:** Panel fill, 1px Hairline bottom border, `5vh` tall, `0 16px`
  padding, 24px logo. Nav links are 15px Read, going Paper White on hover, focus,
  and while their dropdown is open.
- **Dropdown menu:** Inset fill, Hairline border, 8px radius, `--shadow-md`,
  220px minimum width, `8px 0` padding.
- **Menu item:** A three-column row — a fixed 16px icon gutter in Muted, the
  label taking the slack with ellipsis overflow, and the keyboard shortcut ranged
  hard right in 11px Muted tabular-nums. The gutter is drawn whether or not the
  row has an icon, because a menu where only some rows are indented has a ragged
  left edge and the eye reads the ragged edge before it reads any of the words.
  The figure canvas's context menu uses the identical arrangement.
- **Mobile:** None. See The Desktop Rule.

### Toggle Switch

Replaces the OS checkbox for per-channel enable state. A 28×16px pill track at
`rgba(255, 255, 255, 0.16)` with a 12px Paper White knob inset 2px. Checked, the
track becomes Signal Cyan and the knob translates 12px. Hover lightens the track
to `0.26`; hover-while-checked brightens the cyan by 1.1 instead. 120ms on both
the track colour and the knob transform.

### Channel Slot (signature component)

The most-used object in the application — one row per fluorescence channel,
stacked in the sidebar with an 8px gap.

A translucent teal-black card (`rgba(8, 21, 30, 0.76)`) with a barely-there cyan
border (`rgba(56, 189, 248, 0.18)`), 7px radius, and asymmetric padding (`7px 9px
4px 12px`) that leaves room on the left for the identity edge. That edge is a 3px
`::before` bar in `--slot-color`, the colour the user assigned this channel,
rounded `3px 0 0 3px` and transitioning on colour change.

The top row is a five-column grid — `28px 24px minmax(0, 1fr) 24px 24px`: toggle,
colour swatch, channel name (which truncates, never wraps), and two action
buttons. A disabled slot drops to `0.58` opacity rather than hiding, because the
row is how the user turns it back on.

The colour swatch is a 24px square at 6px radius with a
`rgba(255, 255, 255, 0.28)` border, scaling to 1.06 on hover and 0.94 on press,
opening a popover of the ten curated channel colours.

### Tool Card (signature component)

One card per open plugin, reorderable, each carrying its own hue. Inset fill,
Hairline border, 6px radius, and an `inset 3px 0 0` left edge in the tool's
`--tool-accent-quiet`. Selected, the edge goes to full `--tool-accent` and the
border to `--tool-accent-soft` — its own edge at full strength while the others
sit back, rather than a fill or a colour of its own. Several cards are on screen
and only one is selected, so the marker has to read at a glance without making
the rest look switched off and without taking away the colour that identifies
them. A card whose layer is drawing nothing dims its title to Muted; the card
itself stays, because the card is how the user gets it back.

Header row: a Muted grip (`cursor: grab` / `grabbing`), a 12px/700 title that
truncates, and collapse / visibility / remove buttons at 4px gaps.

### Status Indicator (signature component)

Pinned hard right of the top bar on every page, at 12px with a 8px gap.

- **Idle:** A 9px Success Green circle and a Muted label.
- **Busy:** The glyph turns Working Amber and morphs — a 1.6s loop that walks
  `border-radius` from a circle through `34% 66% 62% 38% / 38% 34% 66% 62%` to
  24% and back, rotating a full turn and scaling 1 → 0.8 → 1.06 → 1. It reads as
  one small object changing form. Pure CSS, no SVG and no library. Simultaneously
  the label runs a 1.8s shimmer — a `background-clip: text` gradient sweeping
  Muted → Paper White → Muted across 200% width, like something being typed.
- **Error:** Glyph and label both go Alert Red, no animation.
- **Reduced motion:** Both animations are removed outright rather than run fast —
  they are decorative, and the state is already carried by colour.

### Scrollbars

Scoped to the viewer shell and to the modals that sit outside it, in both the
standard (`scrollbar-color`) and WebKit (`::-webkit-scrollbar`) spellings. Left
to the platform, a Windows scrollbar is a white trough and a grey thumb — the
brightest thing on a near-black page, on a control nobody is looking at. Scoped
to the shell rather than to individual lists, so a plugin's panel matches without
shipping scrollbar chrome of its own.

### The Desk (Figure Builder workspace)

The light room. A 64px icon rail, a 312px thumbnail tray (wide enough for two
thumbnails worth looking at; a third column would come out of the page being
judged), and 12px gaps. Every field on a sidebar is exactly 30px tall
(`--fb-control-h`) — the select, the stepper and the segmented track are three
different constructions and were three different heights, which showed as a
ragged right-hand column. Cards are `--radius-lg` with the two-part Desk float,
and everything that floats over the artwork rather than over the desk — the
context bar, its popovers, the menus, the toast — shares one **Desk pop**
(`0 1px 2px rgba(15, 23, 42, 0.08), 0 10px 30px rgba(15, 23, 42, 0.16)`).

Focus on the Desk is `0 0 0 2px` of `desk-signal` at 0.28 plus the accent on the
control's own border, and it is stated once as `--fb-focus-ring`: viewer.css
loads after the plugin's stylesheet with a bare `:focus-visible` outline, so any
control that does not claim a ring explicitly ends up with a rectangle floating
two pixels clear of itself — or, once that outline is suppressed, with nothing.

Disabled on the Desk is one ink (`#8993a3`, 3.1:1 on white), never an opacity
fade. Only a control that is itself a block of colour — a swatch, a colour well
— fades instead.

## Do's and Don'ts

### Do:

- **Do** pick the room by the subject. Dark chrome where the user is judging a
  backlit image; the Desk rebinding where they are judging ink on paper. Rebind
  core's token names for the subtree rather than writing a parallel stylesheet.
- **Do** reach for a 1px Hairline border and a surface tone step before reaching
  for a shadow.
- **Do** give focus a `0 0 0 3px signal-cyan-soft` ring plus a Signal Cyan
  border, and never remove an outline without replacing it.
- **Do** put identity colour on a 3px left edge, quiet at rest and full-strength
  when active.
- **Do** default to 6px corners, 12px panel padding, 8px gaps, and 11–12px text.
- **Do** use `tabular-nums` on every number that updates in place.
- **Do** transition on 120ms or 180ms with `cubic-bezier(0.2, 0, 0, 1)`, and let
  `prefers-reduced-motion` take it to zero — decorative animation is removed
  outright, not accelerated.
- **Do** truncate long values with an ellipsis. The sidebar sets `overflow-x:
  hidden` explicitly; nothing in it is meant to be reached by scrolling sideways.
- **Do** dim an inactive object to `0.58` opacity or Muted text rather than
  hiding it, when the object itself is the control that brings it back.

### Don't:

- **Don't** add a second chrome accent. If an element needs colour it is a state,
  a channel, or a tool — or it does not need colour.
- **Don't** use `--accent-gate` (`#f36f45`). It predates gating becoming a plugin
  and survives in four declarations; the per-tool hue system replaced it.
- **Don't** tint interface chrome with a channel colour, or force a channel to a
  chrome token. The two palettes stay separate.
- **Don't** fill a card with its identity hue. Eight filled cards in one 380px
  column is a paint chart.
- **Don't** invent a fourth radius. The 2/3/5/7/9/10/14px values already in the
  tree are drift to be reduced, not precedent.
- **Don't** add a ninth button class. Follow the primary/secondary specification
  and consolidate toward it.
- **Don't** introduce a webfont, a CDN reference, an icon package request, or any
  external asset. The no-network constraint is binding and non-negotiable.
- **Don't** put white text on Signal Cyan. Primary buttons carry `#05070a`.
- **Don't** use the dark palette's Alert Red (`#f87171`) or Signal Cyan
  (`#38bdf8`) inside the Desk. On white they become pink and highlighter; use
  `#d64545` and `#2563eb`.
- **Don't** narrow the image to make room. New controls go in the sidebar's
  scroll, over the image, or in a dialog.
- **Don't** build a mobile layout for the viewer. Do keep a 720px window usable.
- **Don't** let a shadow imply a layer that isn't real.
