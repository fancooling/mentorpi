#!/usr/bin/env python3
"""Export both release images and verify their Docker archive before transfer.

Uses the generated release manifest to select transport tags, then hashes actual
exported configuration bytes. Tag reuse or replacement fails before Pi shutdown.
Retains the archive on failure for diagnosis; never contacts the Pi.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path


def verify_export(path, images):
    """Reject an exported pair whose tags/configuration differ from the release.

    Inspect Docker-generated archive metadata without extracting it. Hash raw
    configuration bytes to bind every image to its expected layers and settings.
    This catches retagging between smoke testing and export before Pi cutover.
    """
    expected = {image["local_tag"]: image["image_id"] for image in images.values()}
    with tarfile.open(path) as archive:
        manifest_file = archive.extractfile("manifest.json")
        if manifest_file is None:
            raise RuntimeError("Missing Docker archive manifest")
        with manifest_file:
            manifest = json.load(manifest_file)
        if not isinstance(manifest, list) or len(manifest) != 2:
            raise RuntimeError("Expected exactly two exported images")
        observed = {}
        for item in manifest:
            member = archive.getmember(item["Config"])
            if not member.isfile() or not 0 < member.size <= 4 * 1024 * 1024:
                raise RuntimeError("Invalid exported image configuration")
            with archive.extractfile(member) as config:
                digest = "sha256:" + hashlib.sha256(config.read()).hexdigest()
            for tag in item.get("RepoTags") or []:
                if tag in expected:
                    observed[tag] = digest
        if observed != expected:
            raise RuntimeError(
                "Exported image tags differ from release; rebuild before deployment"
            )


def main():
    """Export RELEASE to OUTPUT and fail unless both configuration digests match."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path, help="Completed build release.json")
    parser.add_argument("output", type=Path, help="New images.tar path")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output archive must not already exist")
    release = json.loads(args.release.read_text())
    tags = [release["images"][role]["local_tag"] for role in ("runtime", "web")]
    if any(not isinstance(tag, str) or not tag or tag.startswith("-") for tag in tags):
        parser.error("Invalid image transport tags in release.json")
    subprocess.run(
        ["docker", "image", "save", "-o", str(args.output), *tags], check=True
    )
    verify_export(args.output, release["images"])


if __name__ == "__main__":
    main()
