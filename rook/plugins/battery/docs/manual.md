# Battery

`battery.status` answers the worker's battery charge (`percent`) and state
(`status`, in the OS's own word: `charging`, `discharging`, `fully-charged`,
...; Termux says it in capitals). The same answer rides the worker's
heartbeat, so the band sees battery levels without anyone calling the cap.

The plugin loads only on a worker that reports a battery. It asks the OS's
own tool, nothing to install:

| OS | Tool |
|---|---|
| Linux | `upower`, or `/sys/class/power_supply/BAT*` when upower isn't there |
| Windows | `Get-CimInstance Win32_Battery` (PowerShell) |
| macOS | `pmset -g batt` |
| Termux | `termux-battery-status` (the Termux:API app) |

The phone app has its own battery plugin (`battery-android`).
