"""Resolve portable image identities across Docker's classic/containerd stores.

The release binds the image configuration digest, including uncompressed layer
identities. Docker's local ID may instead identify a transport manifest. Tags
only discover candidates; verified immutable local IDs are used for execution.
This module uses standard Python and never starts containers or touches hardware.
"""

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path


def archive_config_digest(path):
    """Hash the single image's raw configuration in a Docker-generated archive.

    Read metadata without extracting files. Reject ambiguous archives and oversized
    configs. The config digest binds runtime settings and all rootfs diff IDs;
    Docker validates those layers when loading the image.
    """
    try:
        with tarfile.open(path) as archive:
            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise ValueError("Missing Docker archive manifest")
            with manifest_file:
                manifest = json.load(manifest_file)
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ValueError("Expected one image in Docker archive")
            member = archive.getmember(manifest[0]["Config"])
            if not member.isfile() or not 0 < member.size <= 4 * 1024 * 1024:
                raise ValueError("Invalid image configuration member")
            config_file = archive.extractfile(member)
            if config_file is None:
                raise ValueError("Missing image configuration")
            with config_file:
                config = config_file.read()
            return "sha256:" + hashlib.sha256(config).hexdigest()
    except (KeyError, TypeError, ValueError, tarfile.TarError) as error:
        raise RuntimeError(
            f"Cannot verify exported image configuration: {error}"
        ) from error


def resolve_image(image, verification_cache=None):
    """Verify a release image and return its immutable local Docker ID.

    Discover candidates by config ID, original manifest digest, previously verified
    local IDs, then transport tag. Pin the local ID before exporting and hashing
    the raw configuration; registry pulls need not retain the original build tag.
    A changed tag or platform fails verification. An offline load may reconstruct
    a manifest, so RepoDigests need not equal the original build manifest digest.
    Export uses temporary disk space up to the image's uncompressed archive size,
    allows 15 minutes for slow storage, and removes the archive on exit. No image
    is pulled and no container is created. Errors leave host admission unchanged.
    Optional verification_cache must be a trusted directory: successful digest
    pairs are cached atomically there, avoiding exports on subsequent boots. Both
    keys are immutable digests; local existence and platform are checked each time.
    """
    expected = image["image_id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise RuntimeError("Invalid release image configuration digest")
    references = [expected, image.get("digest")]
    if verification_cache is not None:
        for path in sorted(Path(verification_cache).glob("*.json")):
            try:
                verified = json.loads(path.read_text())
                if (
                    isinstance(verified, dict)
                    and verified.get("config_digest") == expected
                    and isinstance(verified.get("local_id"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", verified["local_id"])
                ):
                    references.append(verified["local_id"])
            except (FileNotFoundError, ValueError):
                pass
    references.append(image.get("local_tag"))
    info = None
    for reference in references:
        if not reference:
            continue
        result = subprocess.run(
            ["docker", "image", "inspect", reference],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)[0]
            break
    if info is None:
        raise RuntimeError(
            f"Release image is not loaded: {image.get('local_tag', expected)}"
        )
    actual = info["Id"]
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", actual)
        or info["Architecture"] != "arm64"
        or info["Os"] != "linux"
    ):
        raise RuntimeError("Loaded image has an invalid identity or platform")
    cached = None
    identity = {"local_id": actual, "config_digest": expected}
    if actual != expected and verification_cache is not None:
        verification_cache = Path(verification_cache)
        cached = verification_cache / (actual.removeprefix("sha256:") + ".json")
        try:
            if json.loads(cached.read_text()) == identity:
                return actual
        except (FileNotFoundError, ValueError):
            pass  # Missing or corrupt cache requires content verification again.
    if actual != expected:
        print(
            f"Verifying portable configuration identity for {image.get('local_tag', actual)}",
            flush=True,
        )
        with tempfile.TemporaryDirectory(prefix="ubuntu-tank-image-") as directory:
            path = Path(directory) / "image.tar"
            result = subprocess.run(
                ["docker", "image", "save", "--output", str(path), actual],
                text=True,
                capture_output=True,
                timeout=900,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Cannot export loaded image: {result.stderr.strip()}"
                )
            if archive_config_digest(path) != expected:
                raise RuntimeError(
                    f"Image configuration digest differs from release: {image.get('local_tag', actual)}"
                )
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=cached.parent) as directory:
            temporary = Path(directory) / "identity.json"
            temporary.write_text(json.dumps(identity))
            temporary.replace(cached)
    return actual
