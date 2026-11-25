"""Publish a verified MotorFree ESP32-C3 application and its OTA manifest."""

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import sleep


REPO = "jiladahe1997/MotorFree-Firmware"
MANIFEST = "motorfree-ota.json"
SLOT_BYTES = 0x180000
SIGNATURE_SECTOR = 4096
APP_DESCRIPTOR = 32
BOARD_DESCRIPTOR = APP_DESCRIPTOR + 256
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"


def image_metadata(image):
    signature_offset = len(image) - SIGNATURE_SECTOR
    if (not 2 * SIGNATURE_SECTOR <= len(image) <= SLOT_BYTES
            or len(image) % SIGNATURE_SECTOR or image[signature_offset:signature_offset + 2] != b"\xe7\x02"):
        raise ValueError("expected an RSA-signed OTA application that fits the app partition")
    project = image[APP_DESCRIPTOR + 48:APP_DESCRIPTOR + 80].split(b"\0", 1)[0]
    if (image[0] != 0xE9 or struct.unpack_from("<H", image, 12)[0] != 5
            or struct.unpack_from("<I", image, APP_DESCRIPTOR)[0] != 0xABCD5432
            or project != b"motorfree"):
        raise ValueError("expected a MotorFree ESP32-C3 application image")

    version = image[APP_DESCRIPTOR + 16:APP_DESCRIPTOR + 48].split(b"\0", 1)[0].decode("ascii")
    board = image[BOARD_DESCRIPTOR:BOARD_DESCRIPTOR + 16].split(b"\0", 1)[0].decode("ascii")
    if len(version) > 24 or not re.fullmatch(VERSION, version) or not re.fullmatch(r"Rev[A-Za-z0-9.-]+", board):
        raise ValueError("invalid firmware version or board in the application descriptor")
    return version, board


def github_release(*args, allow_missing=False):
    result = subprocess.run(
        ["gh", "release", *args, "--repo", REPO], capture_output=True,
        encoding="utf-8", timeout=90, env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )
    if result.returncode == 0:
        return result.stdout.strip()
    message = result.stderr.strip()
    if allow_missing and message == "release not found":
        return None
    raise RuntimeError(message or f"gh release {args[0]} exited with {result.returncode}")


def get_release(*tag):
    result = github_release("view", *tag, "--json", "tagName,assets,isDraft,isPrerelease,url", allow_missing=True)
    return json.loads(result) if result is not None else None


def version_tuple(version):
    return tuple(int(part) for part in version.split("."))


def publish(image, delivery_tag):
    version, board = image_metadata(image)
    if not re.fullmatch(rf"firmware-inbox-v{re.escape(version)}-[0-9]+-[0-9]+", delivery_tag):
        raise ValueError("the delivery tag must match the firmware version")

    latest = get_release()
    if latest:
        match = re.fullmatch(rf"v({VERSION})(?:-build\.[0-9a-f]{{16}})?", latest["tagName"])
        if not match:
            raise ValueError("cannot determine the version of the current latest firmware release")
        if version_tuple(version) <= version_tuple(match[1]):
            print(f"Latest firmware v{match[1]} already covers v{version}: {latest['url']}")
            return

    digest = hashlib.sha256(image).hexdigest()
    tag = f"v{version}-build.{digest[:16]}"
    filename = f"MotorFree-{board}-v{version}.bin"
    manifest = {
        "schema_version": 1,
        "version": version,
        "board": board,
        "tag": tag,
        "filename": filename,
        "size": len(image),
        "sha256": digest,
        "url": f"https://github.com/{REPO}/releases/download/{tag}/{filename}",
    }
    assets = {filename: image, MANIFEST: (json.dumps(manifest, indent=2) + "\n").encode("utf-8")}

    release = get_release(tag)
    if release is None:
        github_release(
            "create", tag, "--draft", "--latest=false", "--target", "main",
            "--title", f"MotorFree Firmware v{version} ({board})",
            "--notes", ("MotorFree ESP32-C3 签名固件。使用旧固件密钥的设备须先通过 USB 烧录一次"
                        "使用当前密钥签名的底包，之后可通过 Android App 进行 OTA 更新。"),
        )
        release = get_release(tag)
        if release is None:
            raise RuntimeError(f"could not find release draft {tag}")

    with TemporaryDirectory(prefix="motorfree-firmware-release-") as directory:
        attached = {asset["name"]: asset for asset in release["assets"]}
        missing = []
        for name, contents in assets.items():
            if name not in attached and release["isDraft"]:
                path = Path(directory) / name
                path.write_bytes(contents)
                missing.append(path)
        if missing:
            github_release("upload", tag, *missing)
            release = get_release(tag)

    attached = {asset["name"]: asset for asset in release["assets"]}
    for name, contents in assets.items():
        asset = attached.get(name)
        expected = f"sha256:{hashlib.sha256(contents).hexdigest()}"
        if not asset or asset["state"] != "uploaded" or asset["size"] != len(contents) or asset["digest"] != expected:
            raise RuntimeError(f"unexpected release asset {name} for {tag}")

    url = github_release("edit", tag, "--draft=false", "--prerelease=false", "--latest=true")
    print(f"Published MotorFree firmware v{version}: {url}")


def publish_with_retry(image, delivery_tag):
    for attempt, delay in enumerate((0, 4, 12)):
        if delay:
            sleep(delay)
        try:
            publish(image, delivery_tag)
            return
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            transient = isinstance(error, subprocess.TimeoutExpired) or re.search(r"\bHTTP 5[0-9]{2}\b", str(error))
            if not transient or attempt == 2:
                raise
            print(f"GitHub request failed. Retrying firmware publication: {error}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: publish_firmware.py signed-motorfree.bin delivery-tag")
    try:
        publish_with_retry(Path(sys.argv[1]).read_bytes(), sys.argv[2])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"MotorFree firmware publish failed: {error}")
