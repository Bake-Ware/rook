# Files

Reads, writes, lists, searches and stats files on this worker, as the
worker's own account. Nothing here starts a process, so these caps never
elevate: for a file the worker can't reach, use `shell.exec` with elevation.

| Cap | What it does |
|---|---|
| `file.read` | A file's content as text (`encoding`, utf-8 by default) or `encoding=base64` for bytes. At most `max_bytes` (8 MiB); a longer file comes back `truncated` with its full `size`. |
| `file.write` | Replaces a file with `content` (or `append=true`). `encoding=base64` writes bytes; `create_parents=true` makes missing directories. |
| `file.list` | A directory's entries: `name`, `type` (file, dir, symlink, other), `size`, `modified`, `mode`. `recursive=true` walks it and adds each entry's relative `path`; `hidden=false` skips dot files; `limit` caps it. |
| `file.search` | Lines matching a regex under `path` (a file or a directory; `glob` narrows the files, binary files are skipped): `[{path, line, text}]`. Stops at `limit` or after 50 seconds and says `truncated`. |
| `file.stat` | Whether a path exists, and its `type`, `size`, `modified` and `mode`. A symlink is reported as itself. |

Errors name the path: `not found: /x`, `permission denied: /x`,
`is a directory: /x`.
