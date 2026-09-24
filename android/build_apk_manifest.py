"""Generate the public /apk.json sidecar from Gradle's universal APK output.

Usage: ROOK_PUBLIC_BASE=https://rook.example.com python3 android/build_apk_manifest.py [output-directory]
Publish rook-worker-apk.json beside rook-worker.apk and update the server's
ROOK_PUBLIC_APK_SHA256 allowlist as one release.
"""
import hashlib
import json
import os
import sys
from pathlib import Path


def build(directory: Path, base: str) -> dict:
    output = json.loads((directory / "output-metadata.json").read_text())
    assert output["applicationId"] == "systems.bake.rook"
    assert len(output["elements"]) == 1, "A universal APK is required"
    entry = output["elements"][0]
    apk = directory / entry["outputFile"]
    assert apk.parent.resolve() == directory.resolve()
    with apk.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    result = {"package": output["applicationId"], "version_code": entry["versionCode"],
              "version_name": entry["versionName"], "sha256": digest,
              "size": apk.stat().st_size, "url": base.rstrip("/") + "/apk"}
    (directory / "rook-worker-apk.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "app/build/outputs/apk/debug"
    base = os.environ.get("ROOK_PUBLIC_BASE")
    if not base:
        sys.exit("Set ROOK_PUBLIC_BASE to the site's public URL (the same value as the APK's ROOK_SERVER)")
    print(json.dumps(build(directory, base)))
