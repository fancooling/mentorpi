#!/usr/bin/env python3
"""Install image-specific APT dependencies, enforce locks and record their closure.

Runs only in disposable image layers. Existing ARM64 versions.lock entries become
APT pins and downloaded archives must match their hashes. New container-only
packages are resolved from signed APT metadata and recorded with exact versions
and hashes. No pip substitution or host preparation is performed.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run(*args: str) -> str:
    """Run an image build command, failing the build on a nonzero exit."""
    try:
        return subprocess.check_output(args, text=True)
    except subprocess.CalledProcessError as exc:
        print(exc.output, file=sys.stderr)
        raise


def main() -> None:
    """Install the requested dependency groups and emit an installed manifest."""
    groups = sys.argv[1:]
    lock = json.loads(Path("/build/versions.json").read_text())
    packages = {p["name"]: p for p in lock["packages"]}
    packages["supervisor"] = {
        "name": "supervisor",
        "version": "4.3.0-1",
        "architecture": "all",
        "sha256": "d1355ccb8b95c54a4804f6f89a3f78567f2c6a4cfaa23cceb2e21c5e1ce14eac",
    }
    roots = json.loads(Path("/build/dependencies.json").read_text())
    pins = []
    for name, pkg in packages.items():
        pins.append(
            f"Package: {name}\nPin: version {pkg['version']}\nPin-Priority: 1001\n"
        )
    Path("/etc/apt/preferences.d/ubuntu-tank").write_text("\n".join(pins))
    selected = sorted({name for group in groups for name in roots[group]})
    requested = [
        f"{n}={packages[n]['version']}" if n in packages else n for n in selected
    ]
    run("apt-get", "update")
    # Keep archives long enough to check and record them before installing.
    Path("/etc/apt/apt.conf.d/docker-clean").unlink(missing_ok=True)
    run(
        "apt-get",
        "install",
        "-y",
        "--download-only",
        "--no-install-recommends",
        "--allow-downgrades",
        *requested,
    )
    archives = []
    for archive in sorted(Path("/var/cache/apt/archives").glob("*.deb")):
        name, version, architecture = run(
            "dpkg-deb", "-f", str(archive), "Package", "Version", "Architecture"
        ).splitlines()
        # dpkg-deb labels fields when multiple fields are requested.
        name, version, architecture = [
            v.split(": ", 1)[-1] for v in (name, version, architecture)
        ]
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if name in packages:
            expected = packages[name]
            if (version, architecture, digest) != (
                expected["version"],
                expected["architecture"],
                expected["sha256"],
            ):
                raise RuntimeError(
                    f"Locked archive mismatch: {name} {version} {architecture}"
                )
        archives.append(
            {
                "name": name,
                "version": version,
                "architecture": architecture,
                "sha256": digest,
            }
        )
    run(
        "apt-get",
        "install",
        "-y",
        "--no-download",
        "--no-install-recommends",
        "--allow-downgrades",
        *requested,
    )
    installed = run("dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n")
    out = Path("/opt/ubuntu_tank/manifests")
    out.mkdir(parents=True, exist_ok=True)
    (out / ("-".join(groups) + ".json")).write_text(
        json.dumps(
            {
                "groups": groups,
                "roots": selected,
                "archives": archives,
                "installed": installed,
            },
            indent=2,
        )
        + "\n"
    )
    run("apt-get", "clean")


if __name__ == "__main__":
    main()
