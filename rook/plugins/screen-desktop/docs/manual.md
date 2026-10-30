# Desktop screenshots

`screen.capture` returns the worker's screen as a JPEG file. `quality`
(1-100, default 85) sets the JPEG quality; `x`, `y`, `w`, `h` together
capture only that region. It is the desktop's answer to the shared
`screen.capture` cap.

The plugin loads only on a worker with a display and uses the first tool
that works:

| OS | Tools, in order |
|---|---|
| Linux | `grim` (Wayland), `scrot` (X11), `spectacle` (KDE), `gnome-screenshot`, ImageMagick `import` |
| macOS | `screencapture` |
| Windows | PowerShell (`System.Drawing`), the whole virtual screen |

A tool that is missing or fails hands over to the next one, so grim failing
on an X11 desktop falls through to scrot. spectacle, gnome-screenshot and
Windows ignore the region and capture the whole screen.

Old name: `screenshot.capture` still works for this release. The `preview`
downscale is gone: ask for a lower `quality` instead.

Right after an install Rook takes one screenshot to check that a tool works
(the plugin rolls back when none does); the picture isn't kept.
