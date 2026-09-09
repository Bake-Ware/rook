"""Generate the public /apk.json sidecar from Gradle's universal APK output.

Usage: python3 android/build_apk_manifest.py [output-directory]
Publish rook-worker-apk.json beside rook-worker.apk and update the server's
ROOK_PUBLIC_APK_SHA256 allowlist as one release.
"""
import hashlib
import json
import sys
from pathlib import Path


def build(directory: Path) -> dict:
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
              "size": apk.stat().st_size, "url": "https://rook.bakeforge.com/apk"}
    (directory / "rook-worker-apk.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "app/build/outputs/apk/debug"
    print(json.dumps(build(directory)))
