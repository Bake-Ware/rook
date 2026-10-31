# Torrents (Deluge)

Drives a Deluge daemon through `deluge-console`, which must be installed on
the worker. The plugin doesn't bundle or start Deluge; the daemon has to be
running for every cap but `torrent.version`, and a cap fails with Deluge's
own message (`Could not connect to daemon ...`, `Password does not match`)
when it can't reach it.

| Cap | What it does |
|---|---|
| `torrent.version` | deluge-console and libtorrent versions; needs no daemon (the post-install check). |
| `torrent.status` | Session summary: rates, DHT nodes, torrents per state. |
| `torrent.list` | Every torrent: state letter, progress, name, 40-hex id. |
| `torrent.get` | One torrent in detail (save path, files). Pull the files with `file.read`. |
| `torrent.add` | Add a magnet link, `.torrent` URL or local path. |
| `torrent.pause` / `torrent.resume` | One torrent, several (hashes separated by spaces), or all (`*`, the default). |
| `torrent.remove` | Remove from Deluge, keep the downloaded files. |
| `torrent.delete` | Remove AND erase the downloaded files. Destructive: the hub asks first. |

State letters in `torrent.list`: S seeding, D downloading, P paused, Q queued,
C checking, E error, M moving, U allocating.

## Identifiers

A torrent is named by its hash (or a prefix of it), several hashes separated
by spaces, or its name. Names can't contain spaces, `;`, quotes, backslashes
or control characters, and can't start with `-`: deluge-console re-parses
its command line, so those would let a name smuggle in a second command or
an option. Use the hash for a torrent whose name has spaces.

## Settings

- `console` (worker): the deluge-console program, default `deluge-console` on PATH.
