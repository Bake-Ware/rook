# Themes

The web dashboard has themes. Pick one under **Settings > My preferences >
Theme**; it is saved for your account (the user-scope setting `ui.theme`) and
applied at once, on every device you sign in from.

| Theme | |
|---|---|
| Rook (`rook-dark`, the default) | the original look: dark olive panels with amber highlights |
| Paper (`paper`) | light: warm paper background, dark ink, deep amber links |
| Graphite (`graphite`) | neutral dark: cool grey panels, a soft blue accent |
| High contrast (`high-contrast`) | black background, white text, bright yellow focus |

Terminals stay dark in every theme, and so does the 3D fleet view.

## The theme format (`rook-theme/1`)

A theme is a JSON file named after its id. The bundled ones are in
`rook/web/themes/`, and the hub serves them at
`/account/ui/themes/<id>.json` so other clients (the desktop app) can use the
same files.

```json
{
 "format": "rook-theme/1",
 "id": "paper",
 "name": "Paper",
 "description": "A light theme: warm paper background, dark ink and deep amber links.",
 "scheme": "light",
 "extends": "rook-dark",
 "tokens": {"bg": "#f3f1ea", "panel": "#fbfaf5", "fg": "#22241e", "accent": "#8a5a12", "...": "..."}
}
```

- `id`: lowercase letters, digits and dashes, up to 40; the file is `<id>.json`.
- `scheme`: `dark` or `light` (sets the CSS `color-scheme`, so form controls
  and scrollbars match).
- `extends`: a theme whose tokens fill in any this one leaves out. Every theme
  but `rook-dark` falls back to `rook-dark`, which must set every token.
- `tokens`: colours by name. Values are `#rgb`, `#rgba`, `#rrggbb`,
  `#rrggbbaa`, or `rgb()`/`rgba()`/`hsl()`/`hsla()` with plain numbers; fonts
  (`sans`, `mono`) are a font-family list. Anything else is refused, so a
  theme cannot inject CSS. Unknown token names are ignored, so an older hub
  reads a newer theme.

### Tokens

| Group | Tokens |
|---|---|
| Surfaces | `bg` page, `sidebar`, `panel` cards, `panel2` inputs and wells, `overlay` floating cards and menus (may be translucent), `hover`, `chip`, `group-bg` group headings, `banner-bg` notices |
| Lines | `line`, `line2` stronger, `line-soft` row separators, `corner` the corner marks on cards |
| Text | `fg`, `fg-soft`, `dim` secondary, `dim2` labels |
| Accent | `accent`, `accent-dim`, `accent-hi` hover, `accent-bg` selected background, `accent-bg-strong` pressed, `accent-line` selected border, `on-accent` text on an accent background |
| Status | `good`, `good-fg`, `good-bg`, `good-line`, `warn`, `warn-bg`, `warn-line`, `bad`, `bad-bg`, `bad-line`, `info` |
| Effects | `shadow`, `backdrop` behind dialogs |
| Terminal | `term-bg`, `term-fg` |
| Artwork | `art-line`, `art-ink`, `art-hatch`, `art-fill`, `art-text`, `art-text2` (the rook drawing behind the pages) |
| 3D view | `scene-1`, `scene-2`, `scene-3` |
| Fonts | `sans`, `mono` |

In CSS each token is a custom property (`var(--accent)`); `--green` and
`--blue` remain as aliases of `good` and `info`.

## How it is applied

- `theme.css` defines the default theme's tokens on `:root`; it is generated
  from `rook-dark.json` (a test keeps the two equal), so a page looks right
  before any script runs.
- `GET /account/ui/api` returns the account's theme with its CSS
  (`:root{color-scheme:...;--bg:...}`), built and checked by the hub
  (rook/hub/plugins/ui/themes.py). `themes.js` puts it in a `<style
  id="rook-theme">` after every stylesheet and remembers it in this browser.
- `theme-boot.js`, a small blocking script on every dashboard page (the
  sign-in page included), applies the remembered theme before the first
  paint, so a light theme never flashes dark.

## Adding a theme

Add `<id>.json` to `rook/web/themes/` and restart the hub: it appears in the
picker and in the `ui.theme` choices. Run the tests (`tests/test_ui_dashboard.py`)
to check it. Themes added by operators at run time, without a restart, are
not supported yet.
