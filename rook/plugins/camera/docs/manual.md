# Cameras

Still photos from a worker's cameras (webcams, a phone's cameras under
Termux). The screen is `screen.capture`; this is a physical camera.

It runs on the hub: the hub lists it on Linux, Termux and Windows workers
that serve `shell.exec`, `shell.env` and `file.read`, runs the tools there,
and reads the photo back from the worker's temp folder (`TMPDIR`, `TEMP` on
Windows, else `/tmp`), removing it after.

| Cap | What it does |
|---|---|
| `camera.list` | The cameras here: `[{camera, name}]`. On Linux `camera` is the N of `/dev/videoN` and `name` the driver's name for it; under Termux 0 is the back camera and 1 the front; on Windows `name` is the DirectShow device name. |
| `camera.capture` | A still JPEG. Linux tries `fswebcam`, then `ffmpeg` (v4l2); Termux uses `termux-camera-photo`; Windows uses `ffmpeg` (DirectShow) with `device` (default `Integrated Camera`). `resolution` is `WxH` (1280x720 by default); `quality` applies to fswebcam. |

Capturing a camera is personal data: the hub may ask before an agent takes
a photo.

Needs one of `fswebcam` or `ffmpeg` on Linux, `termux-api` under Termux, or
`ffmpeg` on Windows. macOS isn't supported.
