# Calendar

The calendars on a phone running the Rook app: what Android's calendar store
holds, which every calendar app syncs into (Google Calendar, Outlook when its
"Sync calendars" setting is on, Samsung, Exchange, CalDAV accounts).

| Cap | What it does |
|---|---|
| `calendar.list` | Events in a window (default: the next 24 hours), recurring ones as single occurrences |
| `calendar.create` | Add an event (default calendar: the primary one the phone may write to) |
| `calendar.update` | Change an event's title, times, place or notes |
| `calendar.delete` | Delete an event for good (the hub asks the person first) |

Times are ISO 8601 (`2026-10-05`, `2026-10-05T14:00`; without an offset they
are the phone's local time) or epoch seconds. All-day events are reported as
dates.

Reading needs the calendar permission (Rook > Settings > Permissions).
Changes also need the phone owner's own switch, off by default: Rook >
Settings > App > "Let the assistant act for me on this phone".
