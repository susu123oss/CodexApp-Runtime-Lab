#!/usr/bin/env python3
"""Offline gates for Candidate -> Laboratory publication. No network writes."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/publish-codex-lab.py"
spec = importlib.util.spec_from_file_location("codex_lab_publisher", SCRIPT)
assert spec and spec.loader
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        # The Mac development tool restricts temporary writes to the project.
        self.temp = tempfile.TemporaryDirectory(prefix="lab-publish-test-", dir=ROOT / ".cache")
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.deb = directory / "wiki.qaq.codex_0.163.0-0_iphoneos-arm64.deb"
        with self.deb.open("wb") as output:
            output.write(b"!<arch>\n")
            output.seek(5 * 1024 * 1024)
            output.write(b"\0")
        self.sums = directory / "SHA256SUMS"
        self.digest = lab.sha256_file(self.deb)
        self.sums.write_text(f"{self.digest}  {self.deb.name}\n")
        self.candidate = "codex-candidate-v0.163.0-a7fec7dd47b3"

    def test_verified_candidate(self) -> None:
        self.assertEqual(lab.verify_candidate(self.deb, self.sums, self.candidate),
                         ("0.163.0-0", self.digest))

    def test_reject_different_upstream(self) -> None:
        with self.assertRaises(ValueError):
            lab.verify_candidate(self.deb, self.sums, "codex-candidate-v0.162.1-a7fec7dd47b3")

    def test_reject_changed_bytes(self) -> None:
        with self.deb.open("r+b") as output:
            output.seek(-1, 2)
            output.write(b"x")
        with self.assertRaises(ValueError):
            lab.verify_candidate(self.deb, self.sums, self.candidate)

    def test_lab_only_manifest_and_no_overwrite(self) -> None:
        manifest = lab.prepare_manifest(lab.base_manifest(), "0.163.0-0", self.deb, self.digest)
        entry = manifest["runtimes"]["codex"]
        self.assertEqual(entry["version"], "v0.163.0-0")
        self.assertIn("/CodexApp-Runtime-Lab/releases/download/", entry["archive_url"])
        self.assertNotIn("/CodexApp-Runtime/releases/", entry["archive_url"])
        self.assertEqual(entry["archive_sha256"], self.digest)
        with self.assertRaises(ValueError):
            lab.prepare_manifest(manifest, "0.163.0-0", self.deb, self.digest)


if __name__ == "__main__":
    unittest.main()
