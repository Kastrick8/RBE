#!/usr/bin/env python3
"""Cross-platform RBE build runner with an interactive TUI and full CLI.

Supports building:
	- Specific RBE components (`backend`, `service`, `cloud-node`, `container`, `rpx`, `rel-lsp`, `sdk-backend`)
	- Complete project-local SDK bundles (`sdk-backend`, `rpx`, `rel-lsp`, and Rust/JS/TS/Python bindings)
	- Cross-OS and cross-architecture Rust target triples (Linux gnu/musl, Windows MSVC, macOS x64/arm64)
	- Toolchain preflight checks (`--check-tools`) and dry-run previews (`--dry-run`)

Uses Python 3.9+ standard library only so it runs everywhere without extra installs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = REPO_ROOT / "engine"
CONTAINER_DIR = REPO_ROOT / "container-runtime"
SDK_SOURCE_DIR = REPO_ROOT / "sdk"
DIST_ROOT = REPO_ROOT / "dist"

# Canonical component registry: maps CLI name -> (workspace_dir, manifest_rel, cargo_pkg, bin_name, dist_name)
COMPONENTS: Dict[str, Tuple[Path, Optional[str], str, str, str]] = {
	"backend": (ENGINE_DIR, None, "backend", "backend", "backend"),
	"service": (ENGINE_DIR, None, "backend", "service", "service"),
	"cloud-node": (ENGINE_DIR, None, "cloud-node", "cloud_node", "cloud_node"),
	"container": (CONTAINER_DIR, None, "container-bin", "container-bin", "container"),
	"rpx": (REPO_ROOT, "engine/crates/rpx/Cargo.toml", "rpx", "rpx", "rpx"),
	"rel-lsp": (ENGINE_DIR, None, "rel-lsp", "rel-lsp", "rel-lsp"),
	"sdk-backend": (REPO_ROOT, "engine/crates/sdk-backend/Cargo.toml", "sdk-backend", "sdk-backend", "backend"),
}

COMPONENT_ORDER = [
	"backend",
	"service",
	"cloud-node",
	"container",
	"rpx",
	"rel-lsp",
	"sdk-backend",
]

SDK_COMPONENTS = ("sdk-backend", "rpx", "rel-lsp")
SUPPORTED_OSES = ("linux", "windows", "macos")
SUPPORTED_ARCHES = ("x64", "arm64", "x86", "armv7")

ALL_RELEASE_TARGETS = (
	"x86_64-pc-windows-msvc",
	"x86_64-unknown-linux-gnu",
	"x86_64-unknown-linux-musl",
	"aarch64-unknown-linux-gnu",
	"aarch64-unknown-linux-musl",
)


@dataclass
class BuildConfig:
	"""Resolved build plan executed by `execute_build`."""

	mode: str = "selective"  # "selective" | "sdk" | "full"
	components: List[str] = field(default_factory=lambda: ["backend"])
	oses: List[str] = field(default_factory=list)
	arches: List[str] = field(default_factory=list)
	custom_targets: List[str] = field(default_factory=list)
	build_all: bool = False
	musl: bool = False
	debug: bool = False
	deny_admin_setup: bool = False
	check_tools: bool = False
	dry_run: bool = False

	@property
	def profile(self) -> str:
		return "debug" if self.debug else "release"


def detect_host_os() -> str:
	"""Maps Python's platform identifier to RBE's normalized OS name."""
	sys_name = platform.system().lower()
	if sys_name.startswith("win"):
		return "windows"
	if sys_name == "darwin":
		return "macos"
	return "linux"


def detect_host_arch() -> str:
	"""Maps Python's machine architecture to RBE's normalized arch name."""
	machine = platform.machine().lower()
	if machine in ("x86_64", "amd64"):
		return "x64"
	if machine in ("aarch64", "arm64"):
		return "arm64"
	if machine in ("i386", "i686", "x86"):
		return "x86"
	if machine.startswith("armv7"):
		return "armv7"
	return "x64"


def resolve_target_triple(target_os: str, arch: str, musl: bool = False) -> str:
	"""Resolves an (os, arch, musl) combination to a Rust target triple."""
	key = (target_os, arch)
	if key == ("windows", "x64"):
		return "x86_64-pc-windows-msvc"
	if key == ("windows", "x86"):
		return "i686-pc-windows-msvc"
	if key == ("windows", "arm64"):
		return "aarch64-pc-windows-msvc"
	if key == ("linux", "x64"):
		return "x86_64-unknown-linux-musl" if musl else "x86_64-unknown-linux-gnu"
	if key == ("linux", "x86"):
		return "i686-unknown-linux-musl" if musl else "i686-unknown-linux-gnu"
	if key == ("linux", "arm64"):
		return "aarch64-unknown-linux-musl" if musl else "aarch64-unknown-linux-gnu"
	if key == ("linux", "armv7"):
		return "armv7-unknown-linux-musleabihf" if musl else "armv7-unknown-linux-gnueabihf"
	if key == ("macos", "x64"):
		return "x86_64-apple-darwin"
	if key == ("macos", "arm64"):
		return "aarch64-apple-darwin"
	raise ValueError(f"Unsupported target combination: {target_os}/{arch}")


def target_os_from_triple(triple: str) -> str:
	"""Extracts the normalized OS name from a Rust target triple."""
	lower = triple.lower()
	if "windows" in lower:
		return "windows"
	if "darwin" in lower:
		return "macos"
	return "linux"


def resolve_targets(cfg: BuildConfig, host_os: Optional[str] = None, host_arch: Optional[str] = None) -> List[str]:
	"""Computes the deduplicated list of Rust target triples for `cfg`."""
	if cfg.custom_targets:
		return list(dict.fromkeys(cfg.custom_targets))

	h_os = host_os or detect_host_os()
	h_arch = host_arch or detect_host_arch()

	if cfg.build_all:
		targets = list(ALL_RELEASE_TARGETS)
		if h_os == "macos":
			targets.extend(["x86_64-apple-darwin", "aarch64-apple-darwin"])
		return targets

	oses = cfg.oses or [h_os]
	arches = cfg.arches or [h_arch]
	resolved: List[str] = []
	for os_name in oses:
		for arch in arches:
			triple = resolve_target_triple(os_name, arch, musl=cfg.musl if os_name == "linux" else False)
			if triple not in resolved:
				resolved.append(triple)
	return resolved


def resolve_shared_cargo_target() -> str:
	"""Resolves RBE's shared Cargo target directory via `build-cache.py`."""
	cache_script = REPO_ROOT / "build-cache.py"
	if cache_script.is_file():
		try:
			out = subprocess.check_output(
				[sys.executable, str(cache_script), str(REPO_ROOT)],
				text=True,
				stderr=subprocess.DEVNULL,
			).strip()
			if out:
				return out
		except Exception:
			pass
	return os.environ.get("CARGO_TARGET_DIR") or str(REPO_ROOT / ".cache" / "rbe-build" / "cargo-target")


def normalize_component(raw: str) -> str:
	"""Normalizes CLI component aliases (e.g. `cloud_node`, `container-bin`)."""
	val = raw.strip().lower().replace("_", "-")
	aliases = {
		"container-bin": "container",
		"lsp": "rel-lsp",
	}
	val = aliases.get(val, val)
	if val not in COMPONENTS:
		supported = ", ".join(COMPONENT_ORDER)
		raise ValueError(f"Unknown component '{raw}'. Supported: {supported}")
	return val


def build_cargo_command(component: str, target: str, release: bool, host_os: Optional[str] = None) -> Tuple[Path, List[str]]:
	"""Builds the `(cwd, argv)` tuple needed to compile `component` for `target`."""
	cwd, manifest_rel, pkg, bin_name, _ = COMPONENTS[component]
	h_os = host_os or detect_host_os()
	tool = "cargo"
	if target_os_from_triple(target) != h_os and shutil.which("cross"):
		tool = "cross"

	cmd = [tool, "build"]
	if manifest_rel:
		cmd.extend(["--manifest-path", str(REPO_ROOT / manifest_rel)])
	else:
		cmd.extend(["-p", pkg])
	cmd.extend(["--bin", bin_name, "--target", target])
	if release:
		cmd.append("--release")
	return cwd, cmd


def run_check_tools() -> int:
	"""Verifies required Rust/system build tools and prints a compact status table."""
	checks = [
		("rustc", ["rustc", "--version"]),
		("cargo", ["cargo", "--version"]),
		("rustup", ["rustup", "--version"]),
		("git", ["git", "--version"]),
		("cc", ["cc", "--version"]),
	]
	print("RBE Build Toolchain Check")
	print("-" * 52)
	ok = True
	for name, cmd in checks:
		exe = shutil.which(cmd[0])
		if not exe:
			# On Windows MSVC, `cl` or `link` may substitute for `cc`.
			if name == "cc" and detect_host_os() == "windows":
				print(f"  {name:<8} [optional on Windows MSVC]")
				continue
			print(f"  {name:<8} MISSING")
			if name in ("rustc", "cargo"):
				ok = False
			continue
		try:
			first_line = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).splitlines()[0]
			print(f"  {name:<8} OK  ({first_line})")
		except Exception as exc:
			print(f"  {name:<8} ERROR ({exc})")
			if name in ("rustc", "cargo"):
				ok = False
	print(f"  cache    {resolve_shared_cargo_target()}")
	return 0 if ok else 1


def ensure_rustup_target(target: str, dry_run: bool) -> None:
	"""Installs `target` via rustup if it is not already present."""
	if dry_run or not shutil.which("rustup"):
		return
	installed = subprocess.check_output(["rustup", "target", "list", "--installed"], text=True).splitlines()
	if target not in installed:
		print(f"  + rustup target add {target}")
		subprocess.run(["rustup", "target", "add", target], check=True)


def copy_clean_tree(source: Path, dest: Path) -> None:
	"""Copies an SDK language binding directory while stripping build/cache clutter."""
	if not source.is_dir():
		raise FileNotFoundError(f"SDK binding directory missing: {source}")
	if dest.exists():
		shutil.rmtree(dest)
	shutil.copytree(
		source,
		dest,
		ignore=shutil.ignore_patterns("target", "node_modules", "__pycache__", ".pytest_cache"),
	)


def copy_sdk_bindings(out_dir: Path) -> None:
	"""Stages Rust, JavaScript, TypeScript, and Python SDK bindings into `<out>/bindings/`."""
	bindings = out_dir / "bindings"
	bindings.mkdir(parents=True, exist_ok=True)
	copy_clean_tree(SDK_SOURCE_DIR / "rbe-sdk", bindings / "rust")
	copy_clean_tree(SDK_SOURCE_DIR / "js", bindings / "javascript")
	copy_clean_tree(SDK_SOURCE_DIR / "js", bindings / "typescript")
	copy_clean_tree(SDK_SOURCE_DIR / "python", bindings / "python")


def execute_build(cfg: BuildConfig) -> int:
	"""Executes the resolved build configuration (or prints it on `--dry-run`)."""
	if cfg.check_tools:
		return run_check_tools()

	targets = resolve_targets(cfg)
	cargo_target_dir = resolve_shared_cargo_target()
	env = os.environ.copy()
	env["CARGO_TARGET_DIR"] = cargo_target_dir
	if cfg.deny_admin_setup:
		env["RBE_DENY_ADMIN_SETUP"] = "1"
		for key in (
			"RBE_ADMIN_PASSWORD",
			"RBE_ADMIN_AUTH_ROUNDS",
			"RBE_ADMIN_AUTH_SALT_HEX",
			"RBE_ADMIN_AUTH_VERIFIER_HEX",
		):
			env.pop(key, None)

	if cfg.mode == "full":
		# Delegate full secure release choreography to the platform release script.
		script = REPO_ROOT / ("build-release.ps1" if detect_host_os() == "windows" else "build-release.sh")
		cmd = (["pwsh", "-File", str(script)] if detect_host_os() == "windows" else ["bash", str(script)])
		if cfg.debug:
			cmd.append("--debug")
		if cfg.musl:
			cmd.append("--musl")
		if cfg.deny_admin_setup:
			cmd.append("--deny-admin-setup")
		if cfg.build_all:
			cmd.append("--build-all")
		print(f"Running full release builder: {' '.join(cmd)}")
		if cfg.dry_run:
			return 0
		return subprocess.run(cmd, cwd=str(REPO_ROOT), env=env).returncode

	selected = list(SDK_COMPONENTS) if cfg.mode == "sdk" and not cfg.components else list(cfg.components)
	if cfg.mode == "sdk":
		for comp in selected:
			if comp not in SDK_COMPONENTS:
				raise ValueError(f"SDK bundle only supports {', '.join(SDK_COMPONENTS)} (got '{comp}')")

	print(f"RBE Build Plan | mode={cfg.mode} | profile={cfg.profile} | cache={cargo_target_dir}")
	print(f"  Targets:    {', '.join(targets)}")
	print(f"  Components: {', '.join(selected)}")

	for target in targets:
		is_win = target_os_from_triple(target) == "windows"
		ext = ".exe" if is_win else ""
		out_dir = DIST_ROOT / target / ("sdk" if cfg.mode == "sdk" else "")
		print(f"\n=== Target: {target} ({cfg.profile}) ===")
		ensure_rustup_target(target, dry_run=cfg.dry_run)

		for comp in selected:
			cwd, cmd = build_cargo_command(comp, target, release=not cfg.debug)
			_, _, _, bin_name, dist_name = COMPONENTS[comp]
			built_path = Path(cargo_target_dir) / target / cfg.profile / f"{bin_name}{ext}"
			dest_path = out_dir / f"{dist_name}{ext}"
			print(f"  [{comp}] {' '.join(cmd)}")
			if cfg.dry_run:
				print(f"    -> would stage {dest_path}")
				continue
			if target_os_from_triple(target) != detect_host_os() and cmd[0] == "cargo":
				print(f"  WARNING: cross-OS target {target} without `cross`; linker may fail", file=sys.stderr)
			subprocess.run(cmd, cwd=str(cwd), env=env, check=True)
			out_dir.mkdir(parents=True, exist_ok=True)
			shutil.copy2(built_path, dest_path)
			print(f"    -> {dest_path}")

		if cfg.mode == "sdk" and set(selected) == set(SDK_COMPONENTS):
			if cfg.dry_run:
				print(f"    -> would stage SDK bindings + sdk-build.json in {out_dir}")
			else:
				copy_sdk_bindings(out_dir)
				manifest = {
					"format": 2,
					"target": target,
					"profile": cfg.profile,
					"sdk_backend": True,
					"rpx": True,
					"rel_lsp": True,
					"bindings": True,
				}
				(out_dir / "sdk-build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
				print(f"    -> {out_dir / 'bindings'} + sdk-build.json")

	print(f"\nBuild complete. Output root: {DIST_ROOT}")
	return 0


# ---------------------------------------------------------------------------
# Interactive Terminal UI (curses with clean cross-platform fallback)
# ---------------------------------------------------------------------------

def _run_fallback_tui(initial: BuildConfig) -> Optional[BuildConfig]:
	"""Zero-dependency numbered menu TUI that works on any terminal (including Windows CMD/PS)."""
	cfg = initial
	if not cfg.oses:
		cfg.oses = [detect_host_os()]
	if not cfg.arches:
		cfg.arches = [detect_host_arch()]

	while True:
		try:
			preview_targets = ", ".join(resolve_targets(cfg))
		except ValueError as err:
			preview_targets = f"INVALID ({err})"
		comps = ", ".join(cfg.components) if cfg.components else "(all SDK)"

		print("\n" + "=" * 62)
		print("  RBE Build Studio (build.py)")
		print("=" * 62)
		print(f"  [1] Mode        : {cfg.mode}  (selective / sdk / full)")
		print(f"  [2] Components  : {comps}")
		print(f"  [3] Target OS   : {', '.join(cfg.oses)}")
		print(f"  [4] Target Arch : {', '.join(cfg.arches)}")
		print(f"  [5] Profile     : {cfg.profile}  |  musl={cfg.musl}  |  deny-admin={cfg.deny_admin_setup}")
		print(f"  [6] Dry Run     : {cfg.dry_run}")
		print("-" * 62)
		print(f"  Resolved triples: {preview_targets}")
		print("-" * 62)
		print("  [b] Build now   [t] Check tools   [q] Quit")
		choice = input("\nSelect option: ").strip().lower()

		if choice == "q":
			return None
		if choice == "t":
			run_check_tools()
			continue
		if choice == "b":
			return cfg
		if choice == "1":
			modes = ["selective", "sdk", "full"]
			cfg.mode = modes[(modes.index(cfg.mode) + 1) % len(modes)]
			if cfg.mode == "sdk":
				cfg.components = list(SDK_COMPONENTS)
		elif choice == "2":
			pool = list(SDK_COMPONENTS) if cfg.mode == "sdk" else COMPONENT_ORDER
			print("  Available:", ", ".join(pool))
			raw = input("  Comma-separated components (blank = keep): ").strip()
			if raw:
				try:
					cfg.components = [normalize_component(part) for part in raw.split(",") if part.strip()]
				except ValueError as err:
					print(f"  Error: {err}")
		elif choice == "3":
			print("  Available OS:", ", ".join(SUPPORTED_OSES))
			raw = input("  Comma-separated OS list: ").strip()
			if raw:
				picked = [p.strip().lower() for p in raw.split(",") if p.strip().lower() in SUPPORTED_OSES]
				if picked:
					cfg.oses = picked
		elif choice == "4":
			print("  Available Arch:", ", ".join(SUPPORTED_ARCHES))
			raw = input("  Comma-separated Arch list: ").strip()
			if raw:
				picked = [p.strip().lower() for p in raw.split(",") if p.strip().lower() in SUPPORTED_ARCHES]
				if picked:
					cfg.arches = picked
		elif choice == "5":
			sub = input("  Toggle (p)rofile, (m)usl, or (a)dmin-deny: ").strip().lower()
			if sub == "p":
				cfg.debug = not cfg.debug
			elif sub == "m":
				cfg.musl = not cfg.musl
			elif sub == "a":
				cfg.deny_admin_setup = not cfg.deny_admin_setup
		elif choice == "6":
			cfg.dry_run = not cfg.dry_run


def _run_curses_tui(initial: BuildConfig) -> Optional[BuildConfig]:
	"""Full-screen keyboard-driven curses TUI (arrows/jk + Space to toggle + Enter to build)."""
	import curses  # Imported lazily so Windows without windows-curses falls back cleanly.

	cfg = initial
	if not cfg.oses:
		cfg.oses = [detect_host_os()]
	if not cfg.arches:
		cfg.arches = [detect_host_arch()]

	def draw(stdscr) -> Optional[BuildConfig]:
		curses.curs_set(0)
		cursor = 0
		modes = ["selective", "sdk", "full"]

		while True:
			pool = list(SDK_COMPONENTS) if cfg.mode == "sdk" else COMPONENT_ORDER
			items: List[Tuple[str, str, str]] = [
				("mode", "mode", f"Mode: {cfg.mode.upper()}  (Space/Enter to cycle: selective / sdk / full)"),
			]
			for comp in pool:
				mark = "x" if comp in cfg.components else " "
				items.append(("comp", comp, f"[{mark}] Component: {comp}"))
			for os_name in SUPPORTED_OSES:
				mark = "x" if os_name in cfg.oses else " "
				items.append(("os", os_name, f"[{mark}] OS: {os_name}"))
			for arch in SUPPORTED_ARCHES:
				mark = "x" if arch in cfg.arches else " "
				items.append(("arch", arch, f"[{mark}] Arch: {arch}"))
			items.append(("opt", "debug", f"[{'x' if cfg.debug else ' '}] Debug profile (default: release)"))
			items.append(("opt", "musl", f"[{'x' if cfg.musl else ' '}] Linux static musl targets"))
			items.append(("opt", "deny_admin", f"[{'x' if cfg.deny_admin_setup else ' '}] --deny-admin-setup"))
			items.append(("opt", "dry_run", f"[{'x' if cfg.dry_run else ' '}] Dry-run (print commands only)"))
			items.append(("action", "build", ">>> START BUILD <<<"))
			items.append(("action", "quit", "Quit"))

			cursor = max(0, min(cursor, len(items) - 1))
			stdscr.clear()
			max_y, max_x = stdscr.getmaxyx()

			title = " RBE Build Studio (Arrows/jk: move | Space: toggle | b: build | q: quit) "
			stdscr.addnstr(0, 0, title.ljust(max_x), max_x - 1, curses.A_REVERSE)

			try:
				preview = ", ".join(resolve_targets(cfg))
			except ValueError as err:
				preview = f"Invalid combo: {err}"
			stdscr.addnstr(2, 2, f"Targets: {preview}", max_x - 4, curses.A_BOLD)

			start_row = 4
			visible_rows = max(1, max_y - start_row - 1)
			scroll = max(0, cursor - visible_rows + 1)

			for idx in range(scroll, min(len(items), scroll + visible_rows)):
				row = start_row + (idx - scroll)
				_, _, label = items[idx]
				attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
				stdscr.addnstr(row, 2, label, max_x - 4, attr)

			stdscr.refresh()
			key = stdscr.getch()

			if key in (ord("q"), 27):
				return None
			if key == ord("b"):
				return cfg
			if key in (curses.KEY_UP, ord("k")):
				cursor = (cursor - 1) % len(items)
			elif key in (curses.KEY_DOWN, ord("j")):
				cursor = (cursor + 1) % len(items)
			elif key in (ord(" "), 10, 13):
				kind, val, _ = items[cursor]
				if kind == "mode":
					cfg.mode = modes[(modes.index(cfg.mode) + 1) % len(modes)]
					if cfg.mode == "sdk":
						cfg.components = list(SDK_COMPONENTS)
				elif kind == "comp":
					if val in cfg.components and len(cfg.components) > 1:
						cfg.components.remove(val)
					elif val not in cfg.components:
						cfg.components.append(val)
				elif kind == "os":
					if val in cfg.oses and len(cfg.oses) > 1:
						cfg.oses.remove(val)
					elif val not in cfg.oses:
						cfg.oses.append(val)
				elif kind == "arch":
					if val in cfg.arches and len(cfg.arches) > 1:
						cfg.arches.remove(val)
					elif val not in cfg.arches:
						cfg.arches.append(val)
				elif kind == "opt":
					if val == "debug":
						cfg.debug = not cfg.debug
					elif val == "musl":
						cfg.musl = not cfg.musl
					elif val == "deny_admin":
						cfg.deny_admin_setup = not cfg.deny_admin_setup
					elif val == "dry_run":
						cfg.dry_run = not cfg.dry_run
				elif kind == "action":
					return cfg if val == "build" else None

	return curses.wrapper(draw)


def launch_tui(initial: BuildConfig) -> Optional[BuildConfig]:
	"""Launches the curses TUI when supported, falling back to the clean text menu."""
	if sys.stdin.isatty() and sys.stdout.isatty():
		try:
			return _run_curses_tui(initial)
		except Exception:
			pass
	return _run_fallback_tui(initial)


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------

def parse_args(argv: Sequence[str]) -> Tuple[BuildConfig, bool]:
	"""Parses CLI flags into `(BuildConfig, should_launch_tui)`."""
	# Expand `--only-<name>` shorthand flags into `--only <name>` so existing habits work seamlessly.
	expanded: List[str] = []
	for token in argv:
		if token.startswith("--only-"):
			expanded.extend(["--only", token[len("--only-"):]])
		else:
			expanded.append(token)

	parser = argparse.ArgumentParser(
		prog="build.py",
		description="Cross-platform RBE builder with interactive TUI and CLI for components, SDK bundles, and OS/arch targets.",
	)
	parser.add_argument("--tui", action="store_true", help="Launch the interactive terminal UI.")
	parser.add_argument("--build-sdk", action="store_true", help="Build the RBE SDK bundle (sdk-backend, rpx, rel-lsp, bindings).")
	parser.add_argument("--full-release", action="store_true", help="Run the full secure release pipeline.")
	parser.add_argument("--only", action="append", default=[], metavar="COMPONENT", help=f"Component to build ({', '.join(COMPONENT_ORDER)}). Repeatable.")
	parser.add_argument("--os", action="append", default=[], choices=SUPPORTED_OSES, help="Target OS (repeatable).")
	parser.add_argument("--arch", action="append", default=[], choices=SUPPORTED_ARCHES, help="Target architecture (repeatable).")
	parser.add_argument("--build-linux", action="store_true", help="Target Linux.")
	parser.add_argument("--build-windows", "--build-win", action="store_true", help="Target Windows.")
	parser.add_argument("--build-macos", action="store_true", help="Target macOS.")
	parser.add_argument("--build-all", action="store_true", help="Build the default multi-platform target matrix.")
	parser.add_argument("--arch-x64", action="store_true", help="Target x86_64.")
	parser.add_argument("--arch-arm64", "--arch-arm", action="store_true", help="Target aarch64.")
	parser.add_argument("--arch-x86", action="store_true", help="Target i686.")
	parser.add_argument("--arch-armv7", action="store_true", help="Target armv7.")
	parser.add_argument("--target", action="append", default=[], metavar="TRIPLE", help="Explicit Rust target triple (repeatable).")
	parser.add_argument("--musl", action="store_true", help="Use Linux musl targets.")
	parser.add_argument("--debug", action="store_true", help="Build debug profile instead of release.")
	parser.add_argument("--deny-admin-setup", action="store_true", help="Skip compile-time admin password prompt.")
	parser.add_argument("--check-tools", action="store_true", help="Verify Rust/C build prerequisites and exit.")
	parser.add_argument("--dry-run", action="store_true", help="Print resolved build commands without executing Cargo.")

	ns = parser.parse_args(expanded)

	oses = list(ns.os)
	if ns.build_linux and "linux" not in oses:
		oses.append("linux")
	if ns.build_windows and "windows" not in oses:
		oses.append("windows")
	if ns.build_macos and "macos" not in oses:
		oses.append("macos")

	arches = list(ns.arch)
	if ns.arch_x64 and "x64" not in arches:
		arches.append("x64")
	if ns.arch_arm64 and "arm64" not in arches:
		arches.append("arm64")
	if ns.arch_x86 and "x86" not in arches:
		arches.append("x86")
	if ns.arch_armv7 and "armv7" not in arches:
		arches.append("armv7")

	components = [normalize_component(c) for c in ns.only]
	if ns.build_sdk:
		mode = "sdk"
		if not components:
			components = list(SDK_COMPONENTS)
	elif ns.full_release:
		mode = "full"
	else:
		mode = "selective"
		if not components:
			components = ["backend"]

	cfg = BuildConfig(
		mode=mode,
		components=components,
		oses=oses,
		arches=arches,
		custom_targets=list(ns.target),
		build_all=ns.build_all,
		musl=ns.musl,
		debug=ns.debug,
		deny_admin_setup=ns.deny_admin_setup,
		check_tools=ns.check_tools,
		dry_run=ns.dry_run,
	)

	# Open TUI automatically when invoked with zero CLI flags on a TTY, or explicitly via `--tui`.
	open_tui = ns.tui or (len(argv) == 0)
	return cfg, open_tui


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = list(sys.argv[1:] if argv is None else argv)
	try:
		cfg, open_tui = parse_args(args)
	except ValueError as err:
		print(f"ERROR: {err}", file=sys.stderr)
		return 2

	if open_tui:
		chosen = launch_tui(cfg)
		if chosen is None:
			print("Build cancelled.")
			return 0
		cfg = chosen

	try:
		return execute_build(cfg)
	except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as err:
		print(f"ERROR: {err}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
