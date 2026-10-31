# Desktop notifications

`notification.post` pops up a notification on the worker's desktop: `text`,
and a `title` (default `Rook`). It is the desktop's answer to the shared
`notification.post` cap, so a notification sent to this worker lands here.

| OS | Tool |
|---|---|
| Linux | `notify-send` (libnotify), shown by the desktop's notification daemon |
| Windows | a tray balloon through PowerShell (`System.Windows.Forms.NotifyIcon`) |
| macOS | `osascript` (`display notification`) |

The plugin loads only on a worker with a display. The text and title are
handed to the tool as data (an argument after `--`, or an environment
variable the script reads), never as code, so any text is safe to send.

0.4's `msg.send` is `notification.post` (no `sender` argument: put it in
the title). The inbox caps (`msg.read`, `msg.clear`) were
dropped: use chat.
