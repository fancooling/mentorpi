"""Check portable config identity when a Buildx exporter omits metadata.

The export fixture produces Docker archive data consumed by the real verifier;
tests neither contact Docker nor exercise Pi installation or hardware.
"""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docker/ubuntu_tank"))

from build import configuration_digest


class BuildConfigurationIdentity(unittest.TestCase):
    """Keep config hashes distinct from local manifest IDs and fail closed."""

    def test_metadata_digest_needs_no_export(self):
        digest = "sha256:" + "a" * 64
        with patch("build.subprocess.run") as export:
            self.assertEqual(
                configuration_digest({"containerimage.config.digest": digest}, "local"),
                digest,
            )
        export.assert_not_called()

    def test_missing_metadata_hashes_exported_config_and_removes_archive(self):
        config = b'{"architecture":"arm64","os":"linux","rootfs":{"diff_ids":[]}}'
        local_id = "sha256:" + "b" * 64
        exported = []

        def export(command, **kwargs):
            self.assertEqual(command[-1], local_id)
            path = Path(command[4])
            exported.append(path)
            with tarfile.open(path, "w") as archive:
                files = {
                    "manifest.json": json.dumps([{"Config": "config.json"}]).encode(),
                    "config.json": config,
                }
                for name, data in files.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))

        with patch("build.subprocess.run", side_effect=export):
            digest = configuration_digest({}, local_id)
        self.assertEqual(digest, "sha256:" + hashlib.sha256(config).hexdigest())
        self.assertNotEqual(digest, local_id)
        self.assertFalse(exported[0].parent.exists())

    def test_invalid_metadata_fails_without_export(self):
        for value in (None, "", "not-a-digest", 7):
            with self.subTest(value=value), patch("build.subprocess.run") as export:
                with self.assertRaisesRegex(RuntimeError, "Invalid Buildx"):
                    configuration_digest(
                        {"containerimage.config.digest": value}, "local"
                    )
                export.assert_not_called()

    def test_export_failure_propagates_and_cleans_up(self):
        paths = []

        def export(command, **kwargs):
            paths.append(Path(command[4]))
            raise subprocess.CalledProcessError(1, command)

        with (
            patch("build.subprocess.run", side_effect=export),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            configuration_digest({}, "sha256:" + "b" * 64)
        self.assertFalse(paths[0].parent.exists())

    def test_bad_archive_fails_and_cleans_up(self):
        paths = []

        def export(command, **kwargs):
            path = Path(command[4])
            paths.append(path)
            path.write_bytes(b"not a Docker image archive")

        with (
            patch("build.subprocess.run", side_effect=export),
            self.assertRaisesRegex(RuntimeError, "Cannot verify exported"),
        ):
            configuration_digest({}, "sha256:" + "b" * 64)
        self.assertFalse(paths[0].parent.exists())


if __name__ == "__main__":
    unittest.main()
