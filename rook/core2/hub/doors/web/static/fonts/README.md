# Fonts

The Rook brand fonts, latin subset, as Google Fonts serves them (the 0.4
dashboard and bake-ware.github.io/rook load the same files from there):

- `dm-sans-latin.woff2`: DM Sans (variable weight), SIL Open Font License 1.1,
  https://github.com/googlefonts/dm-fonts
- `ibm-plex-mono-{400,500,600}-latin.woff2`: IBM Plex Mono, SIL Open Font
  License 1.1, https://github.com/IBM/plex

Kept here so the site needs no network for its look: the shell loads them
through `fonts.css`, and every page (sandboxed, no network) gets them inlined
as `data:` fonts ahead of `page.css`.
