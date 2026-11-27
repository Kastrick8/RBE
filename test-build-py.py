#!/usr/bin/env python3
"""Unit tests for build.py target resolution, CLI parsing, and SDK/component planning."""

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("rbe_build", REPO_ROOT / "build.py")
rbe_build = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["rbe_build"] = rbe_build
SPEC.loader.exec_module(rbe_build)


class BuildPyTests(unittest.TestCase):
	def test_target_triple_resolution(self):
		self.assertEqual(rbe_build.resolve_target_triple("linux", "x64", False), "x86_64-unknown-linux-gnu")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "x64", True), "x86_64-unknown-linux-musl")
		self.assertEqual(rbe_build.resolve_target_triple("windows", "x64", False), "x86_64-pc-windows-msvc")
		self.assertEqual(rbe_build.resolve_target_triple("macos", "arm64", False), "aarch64-apple-darwin")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "armv7", True), "armv7-unknown-linux-musleabihf")

	def test_sdk_bundle_includes_rel_lsp(self):
		cfg, open_tui = rbe_build.parse_args(["--build-sdk", "--build-linux", "--arch-x64", "--dry-run"])
		self.assertFalse(open_tui)
		self.assertEqual(cfg.mode, "sdk")
		self.assertIn("sdk-backend", cfg.components)
		self.assertIn("rpx", cfg.components)
		self.assertIn("rel-lsp", cfg.components)
		self.assertEqual(rbe_build.resolve_targets(cfg), ["x86_64-unknown-linux-gnu"])

	def test_selective_multi_component_and_shorthand_flags(self):
		cfg, _ = rbe_build.parse_args([
			"--only-rel-lsp",
			"--only-backend",
			"--os", "linux",
			"--os", "windows",
			"--arch", "x64",
			"--debug",
		])
		self.assertEqual(cfg.mode, "selective")
		self.assertEqual(cfg.components, ["rel-lsp", "backend"])
		self.assertEqual(cfg.profile, "debug")
		self.assertEqual(
			rbe_build.resolve_targets(cfg),
			["x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"],
		)

	def test_zero_args_defaults_to_tui(self):
		_, open_tui = rbe_build.parse_args([])
		self.assertTrue(open_tui)

	def test_dry_run_exits_cleanly(self):
		rc = rbe_build.main(["--build-sdk", "--build-linux", "--arch-x64", "--dry-run"])
		self.assertEqual(rc, 0)


if __name__ == "__main__":
	unittest.main()
