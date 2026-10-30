"""Plugin manifests (plugin.yaml), format 1 with the 1.1 additions.

Design: knowledge page castling-manifest and its castling-manifest-* section
pages (1.1 changes: castling-manifest-lifecycle). The
structural schema is conformance/manifest/manifest.schema.json; the vectors
beside it pin the semantic checks for other implementations.

    m = load("plugins/pikvm")          # Manifest; raises ManifestError
    issues = validate(data, folder)     # [Issue(path, code, level, message)]
    diff(old, new); check_bump(old, new)
    canonical_json(data); file_hashes(folder); trust_class(folder)

CLI: ``python -m rook.kit.manifest validate <folder>``.
"""

from .diff import Change, check_bump, diff
from .files import canonical_json, file_hashes, trust_class
from .load import Manifest, ManifestError, check, load, normalize
from .types import TYPES, TypeSpecError, compile_fields, compile_type
from .validate import EFFECTS, RISKS, Issue, glob_match, hold_effects, schema, validate

__all__ = [
    "Change", "EFFECTS", "Issue", "Manifest", "ManifestError", "RISKS", "TYPES", "TypeSpecError",
    "canonical_json", "check", "check_bump", "compile_fields", "compile_type", "diff",
    "file_hashes", "glob_match", "hold_effects", "load", "normalize", "schema",
    "trust_class", "validate",
]
