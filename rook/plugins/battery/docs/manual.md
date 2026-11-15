# Battery

`battery.status` answers a worker's battery charge (`percent`) and state
(`status`, in the OS's own word: `charging`, `discharging`, `fully-charged`,
...; Termux says it in capitals).

It runs on the hub: nothing is installed on the worker. The hub lists the cap
on every Linux, Windows and Termux worker that serves `shell.exec`, and runs
the OS's own tool there:

| OS | Tool |
|---|---|
| Linux | `upower`, or `/sys/class/power_supply/BAT*` when upower isn't there |
| Windows | `Get-CimInstance Win32_Battery` (PowerShell) |
| Termux | `termux-battery-status` (the Termux:API app) |

A machine with no battery answers with the tool's own failure (`no battery:
upower reports battery-missing`, `sh exited 3`). The phone app has its own
battery plugin (`battery-android`).
