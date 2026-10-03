#!/usr/bin/env python3
"""Build the paired ARM64 images from an allowlisted context and emit a release.

Run with the repository .venv. Only image build/load and temporary image inspection
are performed; no robot device is mounted, no container application is started,
and no image is pushed. --context-only stages inputs for an external ARM64 builder.
A failed build never writes release.json. Build output must be outside source trees.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from image_identity import archive_config_digest

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = ROOT / "docker/ubuntu_tank"
SCRIPTS = {"build_workspace.sh", "config_migration.py", "fastdds_setup.py"}
BINS = {"mentorpi-tank-run", "mentorpi-tank-lifecycle"}
WEB_ROOT = {
    "package.json",
    "package-lock.json",
    "index.html",
    "tsconfig.json",
    "tsconfig.node.json",
    "vite.config.ts",
}


def run(*args: str) -> str:
    """Run a command from the repository root and return stdout on success."""
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def configuration_digest(metadata: dict, local_image_id: str) -> str:
    """Return the portable config digest even when Buildx omits that metadata.

    Use a valid metadata digest when present. Otherwise export the inspected,
    immutable local image ID and hash its raw config using the shared archive
    verifier. Export needs temporary disk space for the uncompressed image,
    times out after 15 minutes, and is removed on success or failure. Never
    substitute Docker's local ID, which can identify a manifest instead.
    Invalid metadata, export failures and malformed archives fail the build.
    """
    if "containerimage.config.digest" in metadata:
        digest = metadata["containerimage.config.digest"]
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise RuntimeError("Invalid Buildx image configuration digest")
        return digest
    print(
        "Buildx omitted the configuration digest; verifying exported image.", flush=True
    )
    with tempfile.TemporaryDirectory(prefix="ubuntu-tank-build-image-") as directory:
        archive = Path(directory) / "image.tar"
        subprocess.run(
            ["docker", "image", "save", "--output", str(archive), local_image_id],
            check=True,
            timeout=900,
        )
        return archive_config_digest(archive)


def allowed(path: Path) -> bool:
    """Select authored application inputs; exclude caches, keys and developer builds."""
    parts = path.parts
    if any(
        p
        in {"__pycache__", "node_modules", "build", "install", "dist", "tests", "test"}
        or p.endswith(".egg-info")
        for p in parts
    ):
        return False
    if parts[:2] == ("docker", "ubuntu_tank"):
        return path.name in {
            "Dockerfile",
            "dependencies.json",
            "install_dependencies.py",
            "supervisord.conf",
            "runtime-entrypoint.sh",
            "web-entrypoint.sh",
            "runtime_start.py",
            "healthcheck.py",
            "ros-snapshot.asc",
        }
    if parts[:2] == ("ubuntu_tank", "src"):
        return path.suffix not in {".pyc", ".pem", ".key", ".crt"}
    if parts[:2] == ("ubuntu_tank", "scripts"):
        return path.name in SCRIPTS
    if parts[:2] == ("ubuntu_tank", "bin"):
        return path.name in BINS
    if parts[:3] == ("ubuntu_tank", "config", "fastdds"):
        return path.name == "loopback.xml"
    if parts[:2] == ("ubuntu_tank", "web"):
        return (len(parts) == 3 and path.name in WEB_ROOT) or parts[2] in {
            "src",
            "public",
        }
    return path.name in {"LICENSE", "NOTICE"} and len(parts) == 1


def stage(destination: Path) -> dict:
    """Create a new input directory and bind its exact bytes to the Git revision."""
    destination.mkdir(parents=True, exist_ok=False)
    tracked = run("git", "ls-files", "-z").split("\0")
    candidates = {Path(p) for p in tracked if p}
    # Include newly authored C3 inputs before the user commits this implementation.
    candidates.update(p.relative_to(ROOT) for p in CONTAINER.iterdir() if p.is_file())
    # Required C4 package addition is allowlisted before its first commit.
    candidates.add(
        Path("ubuntu_tank/src/ubuntu_tank_protocol/ubuntu_tank_protocol/deployment.py")
    )
    hashes = {}
    for relative in sorted(candidates):
        if not allowed(relative):
            continue
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Build input must be a regular file: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    lock = yaml.safe_load((ROOT / "ubuntu_tank/versions.lock").read_text())
    lock_bytes = (json.dumps(lock, sort_keys=True, indent=2) + "\n").encode()
    (destination / "versions.json").write_bytes(lock_bytes)
    hashes["versions.json"] = hashlib.sha256(lock_bytes).hexdigest()
    identity = {
        "source_revision": run("git", "rev-parse", "HEAD"),
        "source_dirty": bool(run("git", "status", "--porcelain")),
        "context_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
        "inputs": hashes,
        "platform": "linux/arm64",
    }
    (destination / "build-identity.json").write_text(
        json.dumps(identity, indent=2) + "\n"
    )
    return identity


def main() -> None:
    """Stage or build both targets; collect immutable image and dependency identities."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New output directory (retained on failure)",
    )
    parser.add_argument(
        "--context-only", action="store_true", help="Stage inputs without Docker"
    )
    parser.add_argument(
        "--builder", help="Existing Docker buildx builder with ARM64 execution"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Output must not already exist; use a new build directory")
    if output.is_relative_to(ROOT) and not output.is_relative_to(
        ROOT / "ubuntu_tank/.work"
    ):
        parser.error("Repository output must be under ubuntu_tank/.work")
    identity = stage(output / "context")
    if args.context_only:
        print(output / "context")
        return
    sys.path.insert(0, str(ROOT / "ubuntu_tank/src/ubuntu_tank_protocol"))
    sys.path.insert(0, str(ROOT / "ubuntu_tank/scripts"))
    from config_migration import SCHEMA_VERSIONS
    from ubuntu_tank_protocol.constants import (
        SCHEMA_VERSION,
        SUPPORTED_PROTOCOL_VERSIONS,
    )

    release = {
        "format_version": 1,
        **identity,
        "images": {},
        "protocol_versions": SUPPORTED_PROTOCOL_VERSIONS,
        "schema_version": SCHEMA_VERSION,
        "configuration_versions": SCHEMA_VERSIONS,
        "state_format": "ephemeral",
    }
    build_id = identity["context_sha256"][:16]
    for target in ("runtime", "web"):
        tag = f"mentorpi-tank-{target}:build-{build_id}"
        metadata = output / f"{target}-metadata.json"
        command = [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/arm64",
            "--target",
            target,
            "--load",
            "--provenance=false",
            "--metadata-file",
            str(metadata),
            "--tag",
            tag,
            "--build-arg",
            f"SOURCE_REVISION={identity['source_revision']}",
            "-f",
            str(output / "context/docker/ubuntu_tank/Dockerfile"),
        ]
        if args.builder:
            command += ["--builder", args.builder]
        subprocess.run([*command, str(output / "context")], cwd=ROOT, check=True)
        image = json.loads(run("docker", "image", "inspect", tag))[0]
        if image["Architecture"] != "arm64" or image["Os"] != "linux":
            raise RuntimeError(f"{target} is not Linux ARM64")
        build_metadata = json.loads(metadata.read_text())
        digest = build_metadata["containerimage.digest"]
        config_digest = configuration_digest(build_metadata, image["Id"])
        # create/cp reads image files without executing code or attaching hardware.
        container = run("docker", "create", "--entrypoint", "/bin/true", image["Id"])
        manifest_dir = output / target
        try:
            run(
                "docker",
                "cp",
                f"{container}:/opt/ubuntu_tank/manifests",
                str(manifest_dir),
            )
        finally:
            run("docker", "rm", container)
        manifests = {
            p.name: json.loads(p.read_text()) for p in manifest_dir.glob("*.json")
        }
        release["images"][target] = {
            "digest": digest,
            "image_id": config_digest,
            "local_tag": tag,
            "dependencies": manifests,
        }
    release["release_id"] = hashlib.sha256(
        json.dumps(release, sort_keys=True).encode()
    ).hexdigest()
    (output / "release.json").write_text(json.dumps(release, indent=2) + "\n")
    print(output / "release.json")


if __name__ == "__main__":
    main()
