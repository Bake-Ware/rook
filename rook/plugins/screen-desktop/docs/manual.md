# Desktop screenshots

`screen.capture` returns the worker's screen as a JPEG file. `quality`
(1-100, default 85) sets the JPEG quality; `x`, `y`, `w`, `h` together
capture only that region. It is the desktop's answer to the shared
`screen.capture` cap.

It runs on the hub: the hub lists it on every Linux and Windows worker that
serves `shell.exec` and `file.read`, and uses the first tool there that
works:

| OS | Tools, in order |
|---|---|
| Linux | `grim` (Wayland), `scrot` (X11), `spectacle` (KDE), `gnome-screenshot`, ImageMagick `import` |
| Windows | PowerShell (`System.Drawing`), the whole virtual screen |

A tool that is missing or fails hands over to the next one, so grim failing
on an X11 desktop falls through to scrot. On Linux the tool writes a file in
`/tmp` on the worker, which is read back and removed. spectacle,
gnome-screenshot and Windows ignore the region and capture the whole screen;
Windows also ignores `quality`.

0.4's `screenshot.capture` is `screen.capture`. The `preview`
downscale is gone: ask for a lower `quality` instead.
