# Plugin manifest conformance (format 1)

The plugin manifest (`plugin.yaml`) is designed in the knowledge page
`castling-manifest` and its `castling-manifest-*` section pages. The
reference validator is `rook.kit.manifest`
(`python -m rook.kit.manifest validate <folder>`).

Format 1.1 adds optional keys only, so files still say `manifest: 1`; the
list of additions is on `castling-manifest-lifecycle`. `valid/v1-1.yaml`,
`valid/argv-lint.yaml` and `invalid/v1-1*.yaml` cover them.

| File | What |
|---|---|
| `manifest.schema.json` | JSON Schema (2020-12) for the structure. Unknown keys pass it (forward compatibility); a validator reports them as `unknown-key` warnings, or `unknown-action` for an unknown key under a cap's `action`. |
| `valid/*.yaml` | Manifests that load. Some carry warnings. |
| `invalid/*.yaml` | Manifests that must be refused. |
| `expected.json` | For every vector: the issues a validator reports, as `{level, path, code}`. |

## Matching

- `path` is a JSON Pointer into the manifest (`/caps/power.set/args/action`;
  `""` is the whole file). `code` is stable; message text is not normative.
- Schema errors use `schema-<keyword>` (`schema-required`, `schema-pattern`,
  ...) at the failing instance's path. YAML errors are `yaml` at `""`.
- Semantic checks run only when the schema reports no errors.
- Vectors are validated as text with no plugin folder, so file-existence
  checks (`missing-file`) don't apply; path safety (`bad-path`) does.

## The YAML subset

Plain YAML that maps 1:1 to JSON, with plain scalars resolved as in YAML
1.2's core schema: only `true`/`false` are booleans (`on`, `off`, `yes`,
`no` are strings), no dates, no sexagesimal numbers. Refused: anchors and
aliases, custom tags, duplicate keys, non-string keys.

## Canonical form

For hashing and signing: the parsed manifest as JSON with sorted keys, no
whitespace, ASCII-escaped (`canonical_json`). The signed file-hash list is
every file in the plugin folder except `plugin.sig`, as sorted
`path -> sha256`; symlinks are refused.
