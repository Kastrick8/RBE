#!/usr/bin/env python3
"""Unit tests for build.py Rich TUI state machine, multi-arch target matrix, and CLI."""

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
	def test_multi_os_and_multi_arch_target_triples(self):
		self.assertEqual(rbe_build.resolve_target_triple("linux", "x64", False), "x86_64-unknown-linux-gnu")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "x64", True), "x86_64-unknown-linux-musl")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "riscv64", False), "riscv64gc-unknown-linux-gnu")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "riscv64", True), "riscv64gc-unknown-linux-musl")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "loongarch64", False), "loongarch64-unknown-linux-gnu")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "ppc64le", False), "powerpc64le-unknown-linux-gnu")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "s390x", False), "s390x-unknown-linux-gnu")
		self.assertEqual(rbe_build.resolve_target_triple("windows", "arm64", False), "aarch64-pc-windows-msvc")
		self.assertEqual(rbe_build.resolve_target_triple("macos", "arm64", False), "aarch64-apple-darwin")
		self.assertEqual(rbe_build.resolve_target_triple("freebsd", "x64", False), "x86_64-unknown-freebsd")
		self.assertEqual(rbe_build.resolve_target_triple("android", "arm64", False), "aarch64-linux-android")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "wasm32", False), "wasm32-wasip2")
		self.assertEqual(rbe_build.resolve_target_triple("linux", "wasm32", True), "wasm32-unknown-unknown")

	def test_sdk_bundle_includes_rel_lsp(self):
		cfg, open_tui = rbe_build.parse_args(["--build-sdk", "--build-linux", "--arch-arm64", "--dry-run"])
		self.assertFalse(open_tui)
		self.assertEqual(cfg.mode, "sdk")
		self.assertEqual(cfg.components, ["sdk-backend", "rpx", "rel-lsp"])
		self.assertEqual(rbe_build.resolve_targets(cfg), ["aarch64-unknown-linux-gnu"])

	def test_cross_backend_zigbuild_and_custom_targets(self):
		cfg, _ = rbe_build.parse_args([
			"--only-rel-lsp",
			"--target", "x86_64-pc-windows-gnu",
			"--cross-backend", "zigbuild",
			"--features", "extra",
			"-j", "4",
		])
		_, cmd = rbe_build.build_cargo_command("rel-lsp", "x86_64-pc-windows-gnu", cfg, host_os="linux")
		self.assertEqual(cmd[:2], ["cargo", "zigbuild"])
		self.assertIn("--features", cmd)
		self.assertIn("-j", cmd)

	def test_tui_wasd_arrows_e_interact_and_q_subinteract(self):
		state = rbe_build.TuiState(rbe_build.BuildConfig(), host_os="windows", host_arch="x64")
		# Initial cursor is on Build Mode; E cycles selective -> sdk
		state.handle_key("INTERACT")
		self.assertEqual(state.cfg.mode, "sdk")
		self.assertEqual(state.cfg.components, ["sdk-backend", "rpx", "rel-lsp"])
		# Q sub-interacts on Mode to cycle backwards sdk -> selective
		state.handle_key("SUB")
		self.assertEqual(state.cfg.mode, "selective")

		# Move down to first component and press Q to select all components
		state.handle_key("DOWN")
		self.assertEqual(state.focused_row().kind, "comp")
		state.handle_key("SUB")
		self.assertEqual(len(state.cfg.components), len(rbe_build.COMPONENT_ORDER))

		# Jump with RIGHT (D) to Target OS section and press Q to toggle multi-OS cross matrix
		state.handle_key("RIGHT")
		self.assertEqual(state.focused_row().kind, "os")
		state.handle_key("SUB")
		self.assertEqual(state.cfg.oses, ["windows", "linux", "macos"])

		# Jump with RIGHT (D) to Architectures section and press Q -> prompts for custom target triple
		state.handle_key("RIGHT")
		self.assertEqual(state.focused_row().kind, "arch")
		self.assertEqual(state.handle_key("SUB"), "prompt")
		self.assertEqual(state.pending_prompt, "custom_target")

	def test_rich_dashboard_renders_cleanly(self):
		from rich.console import Console

		state = rbe_build.TuiState(rbe_build.BuildConfig(oses=["windows", "linux"], arches=["x64", "arm64", "riscv64"]), host_os="windows", host_arch="x64")
		console = Console(width=120, height=34, record=True)
		console.print(rbe_build.render_rich_dashboard(state))
		rendered = console.export_text()
		self.assertIn("RBE BUILD STUDIO", rendered)
		self.assertIn("riscv64gc-unknown-linux-gnu", rendered)
		self.assertIn("aarch64-pc-windows-msvc", rendered)
		self.assertIn("Interact", rendered)
		self.assertIn("Sub-Action", rendered)

	def test_zero_args_defaults_to_tui_and_dry_run_exits_zero(self):
		_, open_tui = rbe_build.parse_args([])
		self.assertTrue(open_tui)
		rc = rbe_build.main(["--build-sdk", "--os", "linux", "--arch", "arm64", "--arch", "riscv64", "--dry-run"])
		self.assertEqual(rc, 0)


if __name__ == "__main__":
	unittest.main()
