# Phone battery

`battery.status` on a phone running the Rook app: `percent`, `charging`,
`status` (charging, discharging, not charging, full, unknown), `plugged` and
`temp_c`. It reads Android's BatteryManager and needs no permission. The same
answer rides the worker's heartbeat, so the band sees the phone's charge
without anyone calling the cap.

Desktops and Termux use the `battery` plugin; phones cabled to a worker,
`adb`.
