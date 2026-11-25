"""Publish a signed MotorFree APK and advance the Android update manifest."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory


REPO = "jiladahe1997/MotorFree-Firmware"
PACKAGE = "com.motorfree.bletest"
MANIFEST = "motorfree-app.json"
APK_NAME = "MotorFree.apk"
RELEASE_CERT_SHA256 = "f839eef22f23c0c9b98cf531749bd76a0b7e7238e75abaa916325e0d80cb391c"


def gh(*args, allow_missing=False):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120,
                            env={**os.environ, "GH_PROMPT_DISABLED": "1"})
    if result.returncode == 0:
        return result.stdout
    error = result.stderr.strip()
    if allow_missing and (error == "release not found" or "(HTTP 404)" in error):
        return None
    raise RuntimeError(error or f"gh {args[0]} exited with {result.returncode}")


def release(*args, allow_missing=False):
    return gh("release", *args, "--repo", f"github.com/{REPO}", allow_missing=allow_missing)


def apk_metadata(path):
    sdk = Path(os.environ["ANDROID_HOME"])
    tools = sdk / "build-tools/35.0.0"
    aapt = tools / ("aapt.exe" if os.name == "nt" else "aapt")
    apksigner = tools / ("apksigner.bat" if os.name == "nt" else "apksigner")
    badging = subprocess.check_output([aapt, "dump", "badging", path], encoding="utf-8", errors="replace")
    match = re.search(r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([0-9]+\.[0-9]+\.[0-9]+)'",
                      badging, flags=re.MULTILINE)
    if not match or match[1] != PACKAGE or int(match[2]) <= 0 or len(match[3]) > 24:
        raise ValueError("expected a versioned MotorFree APK")

    verified = subprocess.check_output([apksigner, "verify", "--print-certs", path], text=True)
    signers = re.findall(r"certificate SHA-256 digest: ([0-9a-f]+)", verified)
    if signers != [RELEASE_CERT_SHA256]:
        raise ValueError("APK signer does not match the MotorFree release certificate")
    return match[3], int(match[2])


def publish(apk, expected_version):
    contents = apk.read_bytes()
    with TemporaryDirectory(prefix="motorfree-app-release-") as directory:
        attachment = Path(directory) / APK_NAME
        attachment.write_bytes(contents)
        version, version_code = apk_metadata(attachment)
        if version != expected_version:
            raise ValueError("APK version does not match the delivery tag")
        tag = f"app-v{version}"
        sha256 = hashlib.sha256(contents).hexdigest()
        manifest = {"schema_version": 1, "package_name": PACKAGE, "version": version,
                    "version_code": version_code, "tag": tag, "filename": APK_NAME,
                    "size": len(contents), "sha256": sha256}

        index = f"repos/{REPO}/contents/{MANIFEST}"
        result = gh("api", f"{index}?ref=main", allow_missing=True)
        current = json.loads(result) if result else None
        if current:
            previous = json.loads(base64.b64decode(current["content"]))
            if previous["version_code"] >= version_code:
                print(f"Published versionCode {previous['version_code']} already covers this APK ({version_code}).")
                return

        release_fields = ("view", tag, "--json", "isDraft,assets,url")
        found = release(*release_fields, allow_missing=True)
        if found is None:
            release("create", tag, "--target", "main", "--draft", "--prerelease", "--latest=false",
                    "--title", f"MotorFree Android {version}",
                    "--notes", "MotorFree Android APK with the project signing certificate.")
            found = release(*release_fields)
        published = json.loads(found)
        attached = next((asset for asset in published["assets"] if asset["name"] == APK_NAME), None)
        if attached is None and published["isDraft"]:
            release("upload", tag, attachment)
            published = json.loads(release(*release_fields))
            attached = next((asset for asset in published["assets"] if asset["name"] == APK_NAME), None)
        if not attached or attached["state"] != "uploaded" or attached["digest"] != f"sha256:{sha256}":
            raise RuntimeError(f"unexpected APK in release {tag}; each tag identifies one APK")

        url = published["url"]
        if published["isDraft"]:
            url = release("edit", tag, "--draft=false", "--prerelease", "--latest=false").strip()
        if current is None or previous != manifest:
            encoded = base64.b64encode((json.dumps(manifest, indent=2) + "\n").encode()).decode("ascii")
            fields = ["--raw-field", f"sha={current['sha']}"] if current else []
            gh("api", "--method", "PUT", index, "--raw-field", "branch=main",
               "--raw-field", f"message=Publish MotorFree Android {version}", "--raw-field", f"content={encoded}", *fields)
        print(f"MotorFree Android v{version}: {url}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python3 publish_android.py path/to/MotorFree.apk expected-version")
    try:
        publish(Path(sys.argv[1]), sys.argv[2])
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"MotorFree Android publish failed: {error}")
