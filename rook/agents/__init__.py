"""The session host for persistent agents (docs/design/persistent-agents.md 3.2-3.4).

``python -m rook.agents --config <host.json>`` keeps one long-lived Claude Code
process per (role, lane) in the role's directory, talks to the hub over one
websocket (``/home/agents-link``, the host side of the link protocol), runs
side threads (forks, by-the-way questions) and serves the voice lane to the
voice server over the LAN.

It is its own process, not part of the band worker: it survives worker
restarts and OTA updates, holds no band key, and needs only the standard
library and aiohttp.

Modules:

* :mod:`.config`   the host's configuration file
* :mod:`.roledir`  writing what the hub renders into a role directory; session records
* :mod:`.lane`     one Claude Code process and its turns
* :mod:`.side`     forks and by-the-way questions
* :mod:`.voice`    the voice lane websocket server and its turn conductor
* :mod:`.link`     the websocket client to the hub
* :mod:`.host`     the ops the hub sends, and the whole host
"""

from __future__ import annotations

VERSION = "1"
