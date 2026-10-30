# Phone UI

Drive the apps on a phone running the Rook app by what they show, through
the app's accessibility service. Labels work where pixels don't: apps that
block screenshots, screens of any size.

| Cap | What it does |
|---|---|
| `ui.screen` | The screen in compact form: items to act on (with their centre for `hid.click`) and the other text |
| `ui.click` | Click by text, description or view id |
| `ui.scroll` | Scroll a page down or up |
| `ui.type` | Type into a field by its label, without clicking it first |
| `ui.wait` | Wait until something shows, goes, an app is in front, or the screen settles |

Acting and reading in one call: `wait` (seconds) on `ui.click` and `ui.type`
first waits for the target to appear, and `screen: true` returns the screen
once the change has settled, so a click straight after an app opens doesn't
land on the old screen and needs no extra read.

Everything read from the screen is someone else's content (a web page, a
message), so these caps mark the session tainted. Password fields are never
read. `hid.*` taps and swipes at pixels; `screen.capture` takes a picture.

The accessibility service must be on: Rook > Settings > Permissions.
