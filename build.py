#!/usr/bin/env python3
"""Cross-platform RBE build studio with a Rich interactive TUI and full CLI.

Supports:
	- Interactive Rich TUI with Arrows/WASD navigation, E (interact), and Q (sub-action)
	- Selective RBE components (`backend`, `service`, `cloud-node`, `container`, `rpx`, `rel-lsp`, `sdk-backend`)
	- Complete SDK bundles (`sdk-backend`, `rpx`, `rel-lsp`, and Rust/JS/TS/Python bindings)
	- Multi-OS (`windows`, `linux`, `macos`, `freebsd`, `android`) and multi-architecture
	  (`x64`, `arm64`, `x86`, `armv7`, `riscv64`, `loongarch64`, `ppc64le`, `s390x`, `wasm32`)
	  plus arbitrary custom Rust target triples
	- Cross-compilation backends (auto, cargo, cross, zigbuild, xwin); automatic Windows/MSVC builds bootstrap cargo-xwin
	- `check` and `test` modes, custom Cargo features, parallel job limits, and dry-run previews
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = REPO_ROOT / "engine"
CONTAINER_DIR = REPO_ROOT / "container-runtime"
SDK_SOURCE_DIR = REPO_ROOT / "sdk"
DIST_ROOT = REPO_ROOT / "dist"

# Canonical component registry: name -> (workspace_dir, manifest_rel, cargo_pkg, bin_name, dist_name)
COMPONENTS: Dict[str, Tuple[Path, Optional[str], str, str, str]] = {
	"backend": (ENGINE_DIR, None, "backend", "backend", "backend"),
	"service": (ENGINE_DIR, None, "backend", "service", "service"),
	"cloud-node": (ENGINE_DIR, None, "cloud-node", "cloud_node", "cloud_node"),
	"container": (CONTAINER_DIR, None, "container-bin", "container-bin", "container"),
	"rpx": (REPO_ROOT, "engine/crates/rpx/Cargo.toml", "rpx", "rpx", "rpx"),
	"rel-lsp": (ENGINE_DIR, None, "rel-lsp", "rel-lsp", "rel-lsp"),
	"sdk-backend": (REPO_ROOT, "engine/crates/sdk-backend/Cargo.toml", "sdk-backend", "sdk-backend", "backend"),
}

COMPONENT_ORDER = (
	"backend",
	"service",
	"cloud-node",
	"container",
	"rpx",
	"rel-lsp",
	"sdk-backend",
)

SDK_COMPONENTS = ("sdk-backend", "rpx", "rel-lsp")
BUILD_MODES = ("selective", "sdk", "full", "check", "test")
SUPPORTED_OSES = ("windows", "linux", "macos", "freebsd", "android")
SUPPORTED_ARCHES = (
	"x64",
	"arm64",
	"x86",
	"armv7",
	"riscv64",
	"loongarch64",
	"ppc64le",
	"s390x",
	"wasm32",
)
CROSS_BACKENDS = ("auto", "cargo", "cross", "zigbuild", "xwin")
JOB_PRESETS = (None, 2, 4, 8, 16)

ALL_RELEASE_TARGETS = (
	"x86_64-pc-windows-msvc",
	"aarch64-pc-windows-msvc",
	"x86_64-unknown-linux-gnu",
	"x86_64-unknown-linux-musl",
	"aarch64-unknown-linux-gnu",
	"aarch64-unknown-linux-musl",
)


@dataclass
class BuildConfig:
	"""Complete build plan shared between the Rich TUI and non-interactive CLI."""

	mode: str = "selective"  # selective | sdk | full | check | test
	components: List[str] = field(default_factory=lambda: ["backend"])
	oses: List[str] = field(default_factory=list)
	arches: List[str] = field(default_factory=list)
	custom_targets: List[str] = field(default_factory=list)
	build_all: bool = False
	musl: bool = False
	debug: bool = False
	cross_backend: str = "auto"  # auto | cargo | cross | zigbuild
	features: str = ""
	jobs: Optional[int] = None
	deny_admin_setup: bool = False
	no_embed: bool = False
	dev_content: bool = False
	check_tools: bool = False
	dry_run: bool = False

	@property
	def profile(self) -> str:
		return "debug" if self.debug else "release"


def detect_host_os() -> str:
	"""Maps the current OS to RBE's normalized platform name."""
	sys_name = platform.system().lower()
	if sys_name.startswith("win"):
		return "windows"
	if sys_name == "darwin":
		return "macos"
	if sys_name.startswith("freebsd"):
		return "freebsd"
	return "linux"


def detect_host_arch() -> str:
	"""Maps the current CPU architecture to RBE's normalized arch name."""
	machine = platform.machine().lower()
	if machine in ("x86_64", "amd64"):
		return "x64"
	if machine in ("aarch64", "arm64"):
		return "arm64"
	if machine in ("i386", "i686", "x86"):
		return "x86"
	if machine.startswith("armv7"):
		return "armv7"
	if machine.startswith("riscv64"):
		return "riscv64"
	if machine.startswith("loongarch64"):
		return "loongarch64"
	if machine in ("ppc64le", "powerpc64le"):
		return "ppc64le"
	if machine == "s390x":
		return "s390x"
	return "x64"


def resolve_target_triple(target_os: str, arch: str, musl: bool = False) -> str:
	"""Resolves an `(os, arch, musl)` tuple to its canonical Rust target triple."""
	if arch == "wasm32":
		return "wasm32-unknown-unknown" if musl else "wasm32-wasip2"

	table: Dict[Tuple[str, str], Tuple[str, Optional[str]]] = {
		("windows", "x64"): ("x86_64-pc-windows-msvc", None),
		("windows", "arm64"): ("aarch64-pc-windows-msvc", None),
		("windows", "x86"): ("i686-pc-windows-msvc", None),
		("linux", "x64"): ("x86_64-unknown-linux-gnu", "x86_64-unknown-linux-musl"),
		("linux", "arm64"): ("aarch64-unknown-linux-gnu", "aarch64-unknown-linux-musl"),
		("linux", "x86"): ("i686-unknown-linux-gnu", "i686-unknown-linux-musl"),
		("linux", "armv7"): ("armv7-unknown-linux-gnueabihf", "armv7-unknown-linux-musleabihf"),
		("linux", "riscv64"): ("riscv64gc-unknown-linux-gnu", "riscv64gc-unknown-linux-musl"),
		("linux", "loongarch64"): ("loongarch64-unknown-linux-gnu", "loongarch64-unknown-linux-musl"),
		("linux", "ppc64le"): ("powerpc64le-unknown-linux-gnu", None),
		("linux", "s390x"): ("s390x-unknown-linux-gnu", None),
		("macos", "x64"): ("x86_64-apple-darwin", None),
		("macos", "arm64"): ("aarch64-apple-darwin", None),
		("freebsd", "x64"): ("x86_64-unknown-freebsd", None),
		("freebsd", "arm64"): ("aarch64-unknown-freebsd", None),
		("android", "arm64"): ("aarch64-linux-android", None),
		("android", "armv7"): ("armv7-linux-androideabi", None),
		("android", "x64"): ("x86_64-linux-android", None),
	}
	entry = table.get((target_os, arch))
	if entry is None:
		raise ValueError(f"Unsupported target combination: {target_os}/{arch}")
	gnu_triple, musl_triple = entry
	if musl and musl_triple:
		return musl_triple
	return gnu_triple


def target_os_from_triple(triple: str) -> str:
	"""Infers the target OS label from a Rust target triple."""
	lower = triple.lower()
	if "windows" in lower:
		return "windows"
	if "darwin" in lower or "apple" in lower:
		return "macos"
	if "freebsd" in lower:
		return "freebsd"
	if "android" in lower:
		return "android"
	if "wasm" in lower:
		return "wasm"
	return "linux"


def resolve_targets_with_warnings(
	cfg: BuildConfig,
	host_os: Optional[str] = None,
	host_arch: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
	"""Resolves all requested target triples plus any skipped invalid OS/arch pairs."""
	h_os = host_os or detect_host_os()
	h_arch = host_arch or detect_host_arch()
	resolved: List[str] = []
	skipped: List[str] = []

	if cfg.build_all:
		for triple in ALL_RELEASE_TARGETS:
			if triple not in resolved:
				resolved.append(triple)
		if h_os == "macos":
			for triple in ("x86_64-apple-darwin", "aarch64-apple-darwin"):
				if triple not in resolved:
					resolved.append(triple)
	elif cfg.oses or cfg.arches or not cfg.custom_targets:
		oses = cfg.oses or [h_os]
		arches = cfg.arches or [h_arch]
		for os_name in oses:
			for arch in arches:
				try:
					triple = resolve_target_triple(os_name, arch, musl=cfg.musl)
					if triple not in resolved:
						resolved.append(triple)
				except ValueError:
					skipped.append(f"{os_name}/{arch}")

	for custom in cfg.custom_targets:
		cleaned = custom.strip()
		if cleaned and cleaned not in resolved:
			resolved.append(cleaned)

	return resolved, skipped


def resolve_targets(
	cfg: BuildConfig,
	host_os: Optional[str] = None,
	host_arch: Optional[str] = None,
) -> List[str]:
	"""Returns the resolved list of Rust target triples, raising if none are valid."""
	targets, skipped = resolve_targets_with_warnings(cfg, host_os=host_os, host_arch=host_arch)
	if not targets:
		detail = f" (unsupported pairs: {', '.join(skipped)})" if skipped else ""
		raise ValueError(f"No valid Rust target triples resolved{detail}")
	return targets


def resolve_shared_cargo_target() -> str:
	"""Resolves RBE's shared Cargo target cache via `build-cache.py`."""
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
	"""Normalizes CLI component aliases into canonical RBE component names."""
	val = raw.strip().lower().replace("_", "-")
	aliases = {
		"container-bin": "container",
		"lsp": "rel-lsp",
	}
	val = aliases.get(val, val)
	if val not in COMPONENTS:
		raise ValueError(f"Unknown component '{raw}'. Supported: {', '.join(COMPONENT_ORDER)}")
	return val


def is_windows_msvc_target(target: str) -> bool:
	"""Returns whether the target triple is a Windows MSVC target."""
	lower = target.lower()
	return "windows" in lower and "msvc" in lower


def resolve_build_tool(
	target: str,
	backend: str,
	host_os: Optional[str] = None,
	has_cross: Optional[bool] = None,
	has_zigbuild: Optional[bool] = None,
) -> List[str]:
	"""Chooses the Cargo driver; auto uses cargo-xwin for every Windows/MSVC target."""
	h_os = host_os or detect_host_os()
	is_cross = target_os_from_triple(target) != h_os

	if backend == "xwin":
		if not is_windows_msvc_target(target):
			raise ValueError("The xwin backend only supports Windows MSVC target triples.")
		return ["cargo", "xwin"]
	if backend == "cross":
		return ["cross"]
	if backend == "zigbuild":
		return ["cargo", "zigbuild"]
	if backend == "cargo":
		return ["cargo"]

	# xwin provides the MSVC CRT/Windows SDK plus clang-cl/lld-link integration.
	if backend == "auto" and is_windows_msvc_target(target):
		return ["cargo", "xwin"]

	# Auto mode prefers cross or cargo-zigbuild for other cross-OS targets.
	if is_cross:
		if (has_cross if has_cross is not None else bool(shutil.which("cross"))):
			return ["cross"]
		if (has_zigbuild if has_zigbuild is not None else bool(shutil.which("cargo-zigbuild"))):
			return ["cargo", "zigbuild"]
	return ["cargo"]


def build_cargo_command(
	component: str,
	target: str,
	cfg: BuildConfig,
	host_os: Optional[str] = None,
	has_cross: Optional[bool] = None,
	has_zigbuild: Optional[bool] = None,
) -> Tuple[Path, List[str]]:
	"""Constructs `(cwd, argv)` for building, checking, or testing `component`."""
	cwd, manifest_rel, pkg, bin_name, _ = COMPONENTS[component]

	use_xwin = cfg.cross_backend == "xwin" or (
		cfg.cross_backend == "auto" and is_windows_msvc_target(target)
	)
	if cfg.mode in ("check", "test"):
		if use_xwin:
			driver = resolve_build_tool(
				target,
				cfg.cross_backend,
				host_os=host_os,
				has_cross=has_cross,
				has_zigbuild=has_zigbuild,
			)
			cmd = [*driver, cfg.mode]
		else:
			cmd = ["cargo", cfg.mode]
	else:
		driver = resolve_build_tool(
			target,
			cfg.cross_backend,
			host_os=host_os,
			has_cross=has_cross,
			has_zigbuild=has_zigbuild,
		)
		cmd = driver if driver[-1] == "zigbuild" else [*driver, "build"]

	if manifest_rel:
		cmd.extend(["--manifest-path", str(REPO_ROOT / manifest_rel)])
	else:
		cmd.extend(["-p", pkg])

	if cfg.mode not in ("check", "test"):
		cmd.extend(["--bin", bin_name, "--target", target])
		if not cfg.debug:
			cmd.append("--release")
	elif target:
		cmd.extend(["--target", target])

	if cfg.features.strip():
		cmd.extend(["--features", cfg.features.strip()])
	if cfg.jobs:
		cmd.extend(["-j", str(cfg.jobs)])

	return cwd, cmd


def run_check_tools() -> int:
	"""Inspects installed Rust, cross-compilation, and C toolchain prerequisites."""
	checks = [
		("rustc", ["rustc", "--version"]),
		("cargo", ["cargo", "--version"]),
		("rustup", ["rustup", "--version"]),
		("cross", ["cross", "--version"]),
		("zigbuild", ["cargo-zigbuild", "--version"]),
		("cargo-xwin", ["cargo-xwin", "--version"]),
		("clang", ["clang", "--version"]),
		("clang-cl", ["clang-cl", "--version"]),
		("llvm-lib", ["llvm-lib", "--version"]),
		("lld-link", ["lld-link", "--version"]),
		("git", ["git", "--version"]),
		("cc", ["cc", "--version"]),
	]
	print("RBE Build Toolchain Check")
	print("-" * 56)
	ok = True
	for name, cmd in checks:
		if not shutil.which(cmd[0]):
			optional = name in ("cross", "zigbuild", "cargo-xwin", "clang", "clang-cl", "llvm-lib", "lld-link") or (name == "cc" and detect_host_os() == "windows")
			tag = "[optional]" if optional else "MISSING"
			print(f"  {name:<10} {tag}")
			if name in ("rustc", "cargo"):
				ok = False
			continue
		try:
			first_line = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).splitlines()[0]
			print(f"  {name:<10} OK  ({first_line})")
		except Exception as exc:
			print(f"  {name:<10} ERROR ({exc})")
			if name in ("rustc", "cargo"):
				ok = False
	if shutil.which("rustup"):
		try:
			installed = subprocess.check_output(["rustup", "target", "list", "--installed"], text=True).splitlines()
			print(f"  targets    {', '.join(installed)}")
		except Exception:
			pass
	print(f"  cache      {resolve_shared_cargo_target()}")
	return 0 if ok else 1


def ensure_rustup_target(target: str, dry_run: bool) -> None:
	"""Installs `target` via rustup automatically if it is not yet installed."""
	if dry_run or not shutil.which("rustup"):
		return
	installed = subprocess.check_output(["rustup", "target", "list", "--installed"], text=True).splitlines()
	if target not in installed:
		print(f"  + rustup target add {target}")
		subprocess.run(["rustup", "target", "add", target], check=True)


def configure_msvc_llvm_env(target: str, env: Dict[str, str]) -> None:
	"""Defaults MSVC C archiving and final linking to LLVM-compatible tools."""
	if not is_windows_msvc_target(target):
		return
	env_target = target.lower().replace("-", "_")
	# Preserve explicit caller settings; cc-rs reads the target-qualified AR first.
	env.setdefault(f"AR_{env_target}", "llvm-lib")
	env.setdefault(f"CARGO_TARGET_{env_target.upper()}_LINKER", "lld-link")


def ensure_cargo_xwin(env: Dict[str, str], dry_run: bool = False) -> None:
	"""Installs cargo-xwin once and makes CARGO_HOME/bin visible to Cargo subprocesses."""
	cargo_home = Path(env.get("CARGO_HOME") or (Path.home() / ".cargo"))
	cargo_bin = cargo_home / "bin"
	path_entries = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
	cargo_bin_text = str(cargo_bin)
	if cargo_bin_text not in path_entries:
		path_entries.insert(0, cargo_bin_text)
		env["PATH"] = os.pathsep.join(path_entries)

	if shutil.which("cargo-xwin", path=env.get("PATH")):
		return

	install_cmd = ["cargo", "install", "--locked", "cargo-xwin"]
	print(f"  + {' '.join(install_cmd)}")
	if dry_run:
		return
	if not shutil.which("cargo", path=env.get("PATH")):
		raise FileNotFoundError("Cargo is required to bootstrap cargo-xwin; install Rust/Cargo and retry.")
	subprocess.run(install_cmd, env=env, check=True)
	if not shutil.which("cargo-xwin", path=env.get("PATH")):
		raise FileNotFoundError(
			f"cargo install completed but cargo-xwin was not found in {cargo_bin}. "
			"Check CARGO_HOME and ensure its bin directory is on PATH."
		)


def copy_clean_tree(source: Path, dest: Path) -> None:
	"""Copies an SDK language binding directory without build/cache artifacts."""
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


def _is_hex(value: str, length: int) -> bool:
	return bool(re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value))


def derive_admin_verifier(password: str, salt_hex: Optional[str] = None, rounds: int = 120_000) -> Tuple[int, str, str]:
	"""Derives RBE's PBKDF2-HMAC-SHA256 Control Room verifier in pure Python (no OpenSSL required)."""
	if len(password) < 12:
		raise ValueError("Admin password must contain at least 12 characters.")
	if len(password) > 1024:
		raise ValueError("Admin password is unreasonably large.")
	salt = (salt_hex or secrets.token_hex(8)).lower()
	if not _is_hex(salt, 16):
		raise ValueError("RBE_ADMIN_AUTH_SALT_HEX must contain exactly 16 hexadecimal characters.")
	verifier = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds, dklen=32).hex()
	return rounds, salt, verifier


def ensure_admin_verifier_env(env: Dict[str, str], deny_admin_setup: bool, dry_run: bool = False) -> None:
	"""Populates either `RBE_DENY_ADMIN_SETUP=1` or validated `RBE_ADMIN_AUTH_*` env vars."""
	if deny_admin_setup:
		env["RBE_DENY_ADMIN_SETUP"] = "1"
		for key in (
			"RBE_ADMIN_PASSWORD",
			"RBE_ADMIN_AUTH_ROUNDS",
			"RBE_ADMIN_AUTH_SALT_HEX",
			"RBE_ADMIN_AUTH_VERIFIER_HEX",
		):
			env.pop(key, None)
		return

	env.pop("RBE_DENY_ADMIN_SETUP", None)
	salt = env.get("RBE_ADMIN_AUTH_SALT_HEX", "")
	verifier = env.get("RBE_ADMIN_AUTH_VERIFIER_HEX", "")
	rounds_raw = env.get("RBE_ADMIN_AUTH_ROUNDS", "")

	if salt or verifier or rounds_raw:
		if not (salt and verifier and rounds_raw):
			raise ValueError("RBE_ADMIN_AUTH_SALT_HEX, RBE_ADMIN_AUTH_VERIFIER_HEX, and RBE_ADMIN_AUTH_ROUNDS must be supplied together.")
		if not _is_hex(salt, 16) or not _is_hex(verifier, 64) or not rounds_raw.isdigit() or int(rounds_raw) < 10_000:
			raise ValueError("Invalid RBE_ADMIN_AUTH_* verifier parameters.")
		return

	if dry_run:
		return

	password = env.pop("RBE_ADMIN_PASSWORD", "")
	if not password:
		if not sys.stdin.isatty():
			raise ValueError("Set RBE_ADMIN_PASSWORD, pass --deny-admin-setup, or run interactively to set the Control Room password.")
		print("\nRBE Control Room authentication (or pass --deny-admin-setup for first-run setup)")
		first = getpass.getpass("Admin password (12+ characters): ")
		second = getpass.getpass("Confirm admin password: ")
		if first != second:
			raise ValueError("Admin passwords did not match.")
		password = first

	rounds, salt_hex, verifier_hex = derive_admin_verifier(password)
	env["RBE_ADMIN_AUTH_ROUNDS"] = str(rounds)
	env["RBE_ADMIN_AUTH_SALT_HEX"] = salt_hex
	env["RBE_ADMIN_AUTH_VERIFIER_HEX"] = verifier_hex


def ensure_container_signing_key_env(env: Dict[str, str], dry_run: bool = False) -> None:
	"""Ensures a 32-byte (64-hex) ephemeral or externally supplied container signing key in `env`."""
	existing = env.get("RBE_CONTAINER_SIGNING_PRIVATE_KEY", "")
	if existing:
		if not _is_hex(existing, 64):
			raise ValueError("RBE_CONTAINER_SIGNING_PRIVATE_KEY must contain exactly 64 hexadecimal characters.")
		return
	if dry_run:
		return
	key_dir = Path(env.get("RBE_CONFIG_HOME") or (Path.home() / ".rbe"))
	legacy_key = key_dir / "container-signing.key"
	if legacy_key.exists():
		legacy_key.unlink()
	env["RBE_CONTAINER_SIGNING_PRIVATE_KEY"] = secrets.token_hex(32)


def resolve_git_build_id() -> str:
	"""Returns the current Git commit SHA for `RBE_BUILD_ID`, or `unknown-build`."""
	try:
		return subprocess.check_output(
			["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
			text=True,
			stderr=subprocess.DEVNULL,
		).strip() or "unknown-build"
	except Exception:
		return "unknown-build"


def run_full_release(cfg: BuildConfig, targets: List[str], cargo_target_dir: str, env: Dict[str, str]) -> int:
	"""Executes the full RBE release pipeline natively in Python without external shell scripts."""
	ensure_admin_verifier_env(env, deny_admin_setup=cfg.deny_admin_setup, dry_run=cfg.dry_run)
	ensure_container_signing_key_env(env, dry_run=cfg.dry_run)
	env.setdefault("RBE_BUILD_ID", resolve_git_build_id())

	print(f"RBE Full Release Plan | profile={cfg.profile} | cross={cfg.cross_backend} | cache={cargo_target_dir}")
	print(f"  Targets: {', '.join(targets)}")

	for target in targets:
		is_win = target_os_from_triple(target) == "windows"
		ext = ".exe" if is_win else ""
		out_dir = DIST_ROOT / target
		dep_dir = out_dir / "dep"
		print(f"\n=== Full Release: {target} ({cfg.profile}) ===")
		ensure_rustup_target(target, dry_run=cfg.dry_run)

		# 1. Build container-bin -> dist/<target>/dep/container(.exe)
		c_cwd, c_cmd = build_cargo_command("container", target, cfg)
		container_built = Path(cargo_target_dir) / target / cfg.profile / f"container-bin{ext}"
		print(f"  [1/4 container] {' '.join(c_cmd)}")
		if not cfg.dry_run:
			if out_dir.exists():
				shutil.rmtree(out_dir)
			dep_dir.mkdir(parents=True, exist_ok=True)
			subprocess.run(c_cmd, cwd=str(c_cwd), env=env, check=True)
			shutil.copy2(container_built, dep_dir / f"container{ext}")

		# 2. Build service (without embedded path vars)
		s_cwd, s_cmd = build_cargo_command("service", target, cfg)
		service_built = Path(cargo_target_dir) / target / cfg.profile / f"service{ext}"
		print(f"  [2/4 service]   {' '.join(s_cmd)}")
		if not cfg.dry_run:
			s_env = env.copy()
			s_env.pop("RBE_CONTAINER_BIN_PATH", None)
			s_env.pop("RBE_SERVICE_BIN_PATH", None)
			subprocess.run(s_cmd, cwd=str(s_cwd), env=s_env, check=True)
			shutil.copy2(service_built, dep_dir / f"service{ext}")

		# 3. Build cloud_node
		n_cwd, n_cmd = build_cargo_command("cloud-node", target, cfg)
		cloud_built = Path(cargo_target_dir) / target / cfg.profile / f"cloud_node{ext}"
		print(f"  [3/4 cloud]     {' '.join(n_cmd)}")
		if not cfg.dry_run:
			subprocess.run(n_cmd, cwd=str(n_cwd), env=env, check=True)
			shutil.copy2(cloud_built, out_dir / f"cloud_node{ext}")

		# 4. Build backend bound to container & service integrity hashes
		b_cwd, b_cmd = build_cargo_command("backend", target, cfg)
		backend_built = Path(cargo_target_dir) / target / cfg.profile / f"backend{ext}"
		print(f"  [4/4 backend]   {' '.join(b_cmd)}")
		if not cfg.dry_run:
			b_env = env.copy()
			if not cfg.no_embed:
				b_env["RBE_CONTAINER_BIN_PATH"] = str(container_built)
			b_env["RBE_SERVICE_BIN_PATH"] = str(service_built)
			subprocess.run(b_cmd, cwd=str(b_cwd), env=b_env, check=True)
			shutil.copy2(backend_built, out_dir / f"backend{ext}")

			# Stage runtime settings and content folders
			settings = ENGINE_DIR / "settings.json"
			if settings.is_file():
				shutil.copy2(settings, out_dir / "settings.json")
			if cfg.dev_content:
				for folder in ("api", "module"):
					if (REPO_ROOT / folder).is_dir():
						copy_clean_tree(REPO_ROOT / folder, out_dir / folder)
			else:
				(out_dir / "api").mkdir(parents=True, exist_ok=True)
				(out_dir / "module").mkdir(parents=True, exist_ok=True)
			print(f"    -> staged full release in {out_dir}")

	print(f"\nFull release complete. Output root: {DIST_ROOT}")
	return 0


def execute_build(cfg: BuildConfig) -> int:
	"""Executes the resolved build plan (or prints all planned commands on `--dry-run`)."""
	if cfg.check_tools:
		return run_check_tools()

	targets, skipped = resolve_targets_with_warnings(cfg)
	if not targets:
		raise ValueError(f"No valid targets resolved (skipped unsupported: {', '.join(skipped)})")

	cargo_target_dir = resolve_shared_cargo_target()
	env = os.environ.copy()
	env["CARGO_TARGET_DIR"] = cargo_target_dir
	if cfg.cross_backend == "xwin":
		unsupported = [target for target in targets if not is_windows_msvc_target(target)]
		if unsupported:
			detail = ", ".join(unsupported)
			raise ValueError(f"The xwin backend only supports Windows MSVC targets (got: {detail}).")
	if cfg.cross_backend in ("auto", "cargo", "xwin"):
		for target in targets:
			configure_msvc_llvm_env(target, env)
	if cfg.cross_backend == "xwin" or (
		cfg.cross_backend == "auto" and any(is_windows_msvc_target(target) for target in targets)
	):
		ensure_cargo_xwin(env, dry_run=cfg.dry_run)
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
		return run_full_release(cfg, targets, cargo_target_dir, env)

	selected = list(SDK_COMPONENTS) if cfg.mode == "sdk" and not cfg.components else list(cfg.components)
	if cfg.mode == "sdk":
		for comp in selected:
			if comp not in SDK_COMPONENTS:
				raise ValueError(f"SDK mode only supports {', '.join(SDK_COMPONENTS)} (got '{comp}')")

	print(f"RBE Build Plan | mode={cfg.mode} | profile={cfg.profile} | cross={cfg.cross_backend} | cache={cargo_target_dir}")
	print(f"  Targets:    {', '.join(targets)}")
	if skipped:
		print(f"  Skipped:    {', '.join(skipped)} (unsupported OS/arch pairs)")
	print(f"  Components: {', '.join(selected)}")

	for target in targets:
		is_win = target_os_from_triple(target) == "windows"
		ext = ".exe" if is_win else ""
		out_dir = DIST_ROOT / target / ("sdk" if cfg.mode == "sdk" else "")
		print(f"\n=== Target: {target} ({cfg.profile}) ===")
		ensure_rustup_target(target, dry_run=cfg.dry_run)

		for comp in selected:
			cwd, cmd = build_cargo_command(comp, target, cfg)
			_, _, _, bin_name, dist_name = COMPONENTS[comp]
			built_path = Path(cargo_target_dir) / target / cfg.profile / f"{bin_name}{ext}"
			dest_path = out_dir / f"{dist_name}{ext}"
			print(f"  [{comp}] {' '.join(cmd)}")
			if cfg.dry_run:
				if cfg.mode not in ("check", "test"):
					print(f"    -> would stage {dest_path}")
				continue
			if cfg.mode not in ("check", "test") and target_os_from_triple(target) != detect_host_os() and cmd[0] == "cargo" and "zigbuild" not in cmd and "xwin" not in cmd:
				print(f"  WARNING: cross-OS target {target} without `cross` or `cargo-zigbuild`; linker may fail", file=sys.stderr)
			subprocess.run(cmd, cwd=str(cwd), env=env, check=True)
			if cfg.mode not in ("check", "test"):
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

	print(f"\nDone. Output root: {DIST_ROOT}")
	return 0


# ---------------------------------------------------------------------------
# Cross-Platform Single-Keypress Input (Arrows + WASD + E + Q + Esc)
# ---------------------------------------------------------------------------

TUI_FPS = 30
TUI_TICK_SECONDS = 1.0 / TUI_FPS


class KeyPoller:
	"""Non-blocking single-keypress poller running at 30 Hz on Windows and Unix/macOS.

	Keeps Unix stdin in cbreak/raw mode for the lifetime of the TUI session instead
	of toggling termios on every keypress, and uses `msvcrt.kbhit()` on Windows so
	the 30 FPS UI loop stays snappy with zero input lag.
	"""

	def __init__(self) -> None:
		self._fd: Optional[int] = None
		self._old_termios = None

	def __enter__(self) -> "KeyPoller":
		if os.name != "nt" and sys.stdin.isatty():
			import termios
			import tty

			self._fd = sys.stdin.fileno()
			self._old_termios = termios.tcgetattr(self._fd)
			tty.setraw(self._fd)
		return self

	def __exit__(self, *_exc) -> None:
		if os.name != "nt" and self._fd is not None and self._old_termios is not None:
			import termios

			termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
			self._fd = None
			self._old_termios = None

	def poll_all(self, timeout: float = TUI_TICK_SECONDS) -> List[str]:
		"""Waits up to `timeout` seconds for input, then drains all buffered keys."""
		keys: List[str] = []
		if os.name == "nt":
			import msvcrt

			deadline = time.monotonic() + timeout
			while not msvcrt.kbhit():
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					return keys
				time.sleep(min(0.004, remaining))
			while msvcrt.kbhit():
				ch = msvcrt.getwch()
				if ch in ("\x00", "\xe0"):
					code = msvcrt.getwch()
					k = {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(code, "")
				else:
					k = _normalize_key_char(ch)
				if k:
					keys.append(k)
			return keys

		import select

		if self._fd is None:
			return keys
		ready, _, _ = select.select([self._fd], [], [], timeout)
		if not ready:
			return keys
		while select.select([self._fd], [], [], 0.0)[0]:
			ch = os.read(self._fd, 1).decode("utf-8", errors="ignore")
			if not ch:
				break
			if ch == "\x1b":
				if select.select([self._fd], [], [], 0.015)[0]:
					seq = os.read(self._fd, 2).decode("utf-8", errors="ignore")
					k = {"[A": "UP", "[B": "DOWN", "[D": "LEFT", "[C": "RIGHT"}.get(seq, "ESC")
				else:
					k = "ESC"
			else:
				k = _normalize_key_char(ch)
			if k:
				keys.append(k)
		return keys


def _normalize_key_char(ch: str) -> str:
	"""Maps raw ASCII characters to logical TUI navigation and action tokens."""
	if ch in ("\x03", "\x1b"):
		return "ESC"
	if ch in ("\r", "\n"):
		return "INTERACT"
	if ch == " ":
		return "INTERACT"
	low = ch.lower()
	mapping = {
		"w": "UP",
		"s": "DOWN",
		"a": "LEFT",
		"d": "RIGHT",
		"e": "INTERACT",
		"q": "SUB",
		"b": "BUILD",
		"t": "TOOLS",
	}
	return mapping.get(low, "")


# ---------------------------------------------------------------------------
# Rich Interactive TUI State Machine & Renderer
# ---------------------------------------------------------------------------

@dataclass
class MenuRow:
	"""One navigable item in the Rich Build Studio menu."""

	kind: str  # mode | comp | os | arch | custom | opt | action
	key: str
	label: str
	sub_hint: str = ""


class TuiState:
	"""Pure-Python state controller for the Rich TUI (easy to drive in unit tests)."""

	def __init__(self, cfg: BuildConfig, host_os: Optional[str] = None, host_arch: Optional[str] = None) -> None:
		self.cfg = cfg
		self.host_os = host_os or detect_host_os()
		self.host_arch = host_arch or detect_host_arch()
		if not self.cfg.oses:
			self.cfg.oses = [self.host_os]
		if not self.cfg.arches:
			self.cfg.arches = [self.host_arch]
		self.cursor = 0
		self.status_msg = "Arrows/WASD: move  |  E: interact/toggle  |  Q: sub-action"
		self.pending_prompt: Optional[str] = None  # "custom_target" | "features"
		# Probe cache path and installed tools ONCE at startup so 30 FPS rendering never spawns subprocesses.
		self.cargo_target_dir = resolve_shared_cargo_target()
		self.detected_tools = [
			name for name in ("rustc", "cargo", "rustup", "cross", "cargo-zigbuild", "cargo-xwin", "clang", "clang-cl", "llvm-lib", "lld-link") if shutil.which(name)
		]
		self.has_cross = "cross" in self.detected_tools
		self.has_zigbuild = "cargo-zigbuild" in self.detected_tools

	def rows(self) -> List[MenuRow]:
		"""Builds the ordered list of interactive rows for the active mode."""
		items: List[MenuRow] = [
			MenuRow("mode", "mode", "Build Mode", "Q: cycle backwards"),
		]
		comp_pool = SDK_COMPONENTS if self.cfg.mode == "sdk" else COMPONENT_ORDER
		for comp in comp_pool:
			items.append(MenuRow("comp", comp, comp, "Q: select all / reset"))
		for os_name in SUPPORTED_OSES:
			items.append(MenuRow("os", os_name, os_name, "Q: toggle multi-OS matrix"))
		for arch in SUPPORTED_ARCHES:
			items.append(MenuRow("arch", arch, arch, "Q: add custom target triple"))
		items.append(MenuRow("custom", "custom_targets", "Custom Target Triples", "E: add triple  |  Q: clear all"))
		items.extend([
			MenuRow("opt", "profile", "Profile (release / debug)", "Q: toggle musl"),
			MenuRow("opt", "musl", "Linux Static musl / Bare WASM", "Q: toggle profile"),
			MenuRow("opt", "cross_backend", "Cross Backend (auto/cargo/cross/zigbuild/xwin)", "Q: cycle backwards"),
			MenuRow("opt", "build_all", "Full Release Matrix (--build-all)", "Q: toggle dry-run"),
			MenuRow("opt", "features", "Cargo Features (--features)", "E/Q: edit or clear"),
			MenuRow("opt", "jobs", "Parallel Jobs (-j)", "Q: reset to auto"),
			MenuRow("opt", "deny_admin", "--deny-admin-setup", "Q: toggle --no-embed"),
			MenuRow("opt", "no_embed", "--no-embed (full release)", "Q: toggle --dev-content"),
			MenuRow("opt", "dev_content", "--dev-content (full release)", "Q: toggle dry-run"),
			MenuRow("opt", "dry_run", "Dry-Run Preview (--dry-run)", "Q: run build now"),
			MenuRow("action", "build", "Start Build", "E: execute build plan"),
			MenuRow("action", "tools", "Check Toolchain & Targets", "E: inspect Rust, xwin, and LLVM tools"),
			MenuRow("action", "quit", "Quit", "Esc: exit immediately"),
		])
		return items

	def focused_row(self) -> MenuRow:
		rows = self.rows()
		self.cursor = max(0, min(self.cursor, len(rows) - 1))
		return rows[self.cursor]

	def handle_key(self, key: str) -> Optional[str]:
		"""Processes one logical key. Returns `'build'`, `'tools'`, `'quit'`, `'prompt'`, or `None`."""
		rows = self.rows()
		self.cursor = max(0, min(self.cursor, len(rows) - 1))

		if key == "ESC":
			return "quit"
		if key == "BUILD":
			return "build"
		if key == "TOOLS":
			return "tools"
		if key == "UP":
			self.cursor = (self.cursor - 1) % len(rows)
			return None
		if key == "DOWN":
			self.cursor = (self.cursor + 1) % len(rows)
			return None

		row = rows[self.cursor]

		if key in ("LEFT", "RIGHT"):
			step = -1 if key == "LEFT" else 1
			return self._cycle_row(row, step)

		if key == "INTERACT":
			return self._interact_row(row)

		if key == "SUB":
			return self._sub_interact_row(row)

		return None

	def _cycle_row(self, row: MenuRow, step: int) -> None:
		if row.kind == "mode":
			idx = (BUILD_MODES.index(self.cfg.mode) + step) % len(BUILD_MODES)
			self.cfg.mode = BUILD_MODES[idx]
			if self.cfg.mode == "sdk":
				self.cfg.components = list(SDK_COMPONENTS)
			self.status_msg = f"Switched mode -> {self.cfg.mode}"
		elif row.kind == "opt" and row.key == "cross_backend":
			idx = (CROSS_BACKENDS.index(self.cfg.cross_backend) + step) % len(CROSS_BACKENDS)
			self.cfg.cross_backend = CROSS_BACKENDS[idx]
			self.status_msg = f"Cross backend -> {self.cfg.cross_backend}"
		elif row.kind == "opt" and row.key == "jobs":
			idx = (JOB_PRESETS.index(self.cfg.jobs) + step) % len(JOB_PRESETS)
			self.cfg.jobs = JOB_PRESETS[idx]
			self.status_msg = f"Parallel jobs -> {self.cfg.jobs or 'auto'}"
		elif row.kind == "opt" and row.key == "profile":
			self.cfg.debug = not self.cfg.debug
			self.status_msg = f"Profile -> {self.cfg.profile}"
		else:
			# On checkbox rows, A/D jumps cleanly to the previous/next section.
			rows = self.rows()
			cur_kind = row.kind
			idx = self.cursor
			for _ in range(len(rows)):
				idx = (idx + step) % len(rows)
				if rows[idx].kind != cur_kind:
					self.cursor = idx
					self.status_msg = f"Jumped to section: {rows[idx].kind}"
					break

	def _interact_row(self, row: MenuRow) -> Optional[str]:
		if row.kind == "mode":
			self._cycle_row(row, 1)
			return None
		if row.kind == "comp":
			if row.key in self.cfg.components and len(self.cfg.components) > 1:
				self.cfg.components.remove(row.key)
			elif row.key not in self.cfg.components:
				self.cfg.components.append(row.key)
			self.status_msg = f"Components: {', '.join(self.cfg.components)}"
			return None
		if row.kind == "os":
			if row.key in self.cfg.oses and len(self.cfg.oses) > 1:
				self.cfg.oses.remove(row.key)
			elif row.key not in self.cfg.oses:
				self.cfg.oses.append(row.key)
			self.status_msg = f"Target OSes: {', '.join(self.cfg.oses)}"
			return None
		if row.kind == "arch":
			if row.key in self.cfg.arches and len(self.cfg.arches) > 1:
				self.cfg.arches.remove(row.key)
			elif row.key not in self.cfg.arches:
				self.cfg.arches.append(row.key)
			self.status_msg = f"Architectures: {', '.join(self.cfg.arches)}"
			return None
		if row.kind == "custom":
			self.pending_prompt = "custom_target"
			return "prompt"
		if row.kind == "opt":
			if row.key == "profile":
				self.cfg.debug = not self.cfg.debug
			elif row.key == "musl":
				self.cfg.musl = not self.cfg.musl
			elif row.key == "cross_backend":
				self._cycle_row(row, 1)
			elif row.key == "build_all":
				self.cfg.build_all = not self.cfg.build_all
			elif row.key == "features":
				self.pending_prompt = "features"
				return "prompt"
			elif row.key == "jobs":
				self._cycle_row(row, 1)
			elif row.key == "deny_admin":
				self.cfg.deny_admin_setup = not self.cfg.deny_admin_setup
			elif row.key == "no_embed":
				self.cfg.no_embed = not self.cfg.no_embed
			elif row.key == "dev_content":
				self.cfg.dev_content = not self.cfg.dev_content
			elif row.key == "dry_run":
				self.cfg.dry_run = not self.cfg.dry_run
			return None
		if row.kind == "action":
			return row.key
		return None

	def _sub_interact_row(self, row: MenuRow) -> Optional[str]:
		"""Context-sensitive `Q` sub-interaction for rapid multi-select and custom targets."""
		if row.kind == "mode":
			self._cycle_row(row, -1)
			return None
		if row.kind == "comp":
			pool = list(SDK_COMPONENTS) if self.cfg.mode == "sdk" else list(COMPONENT_ORDER)
			if len(self.cfg.components) == len(pool):
				self.cfg.components = [pool[0]]
				self.status_msg = f"Reset components -> {pool[0]}"
			else:
				self.cfg.components = pool
				self.status_msg = f"Selected all {len(pool)} components"
			return None
		if row.kind == "os":
			cross_set = ["windows", "linux", "macos"]
			if set(self.cfg.oses) == set(cross_set):
				self.cfg.oses = [self.host_os]
				self.status_msg = f"Reset OS -> {self.host_os}"
			else:
				self.cfg.oses = cross_set
				self.status_msg = "Enabled cross-platform OS matrix (windows, linux, macos)"
			return None
		if row.kind == "arch":
			self.pending_prompt = "custom_target"
			return "prompt"
		if row.kind == "custom":
			self.cfg.custom_targets.clear()
			self.status_msg = "Cleared custom target triples"
			return None
		if row.kind == "opt":
			if row.key == "profile":
				self.cfg.musl = not self.cfg.musl
				self.status_msg = f"musl/bare-wasm -> {self.cfg.musl}"
			elif row.key == "musl":
				self.cfg.debug = not self.cfg.debug
				self.status_msg = f"Profile -> {self.cfg.profile}"
			elif row.key == "cross_backend":
				self._cycle_row(row, -1)
			elif row.key == "features":
				self.cfg.features = ""
				self.status_msg = "Cleared Cargo features"
			elif row.key == "jobs":
				self.cfg.jobs = None
				self.status_msg = "Reset parallel jobs -> auto"
			elif row.key == "dry_run":
				return "build"
			else:
				self.cfg.dry_run = not self.cfg.dry_run
				self.status_msg = f"Dry-run -> {self.cfg.dry_run}"
			return None
		if row.kind == "action" and row.key == "build":
			self.cfg.dry_run = not self.cfg.dry_run
			self.status_msg = f"Toggled dry-run -> {self.cfg.dry_run}"
		return None


def render_rich_dashboard(state: TuiState):
	"""Renders the full two-column Rich TUI layout for `state`."""
	from rich import box
	from rich.layout import Layout
	from rich.panel import Panel
	from rich.table import Table
	from rich.text import Text

	cfg = state.cfg
	rows = state.rows()
	focused = state.focused_row()
	targets, skipped = resolve_targets_with_warnings(cfg, host_os=state.host_os, host_arch=state.host_arch)

	# Header bar
	header_text = Text()
	header_text.append(" RBE BUILD STUDIO ", style="bold black on bright_cyan")
	header_text.append(f"  host={state.host_os}/{state.host_arch}", style="cyan")
	header_text.append(f"  mode={cfg.mode.upper()}", style="bold magenta")
	header_text.append(f"  profile={cfg.profile}", style="bold green" if not cfg.debug else "bold yellow")
	if cfg.dry_run:
		header_text.append("  [DRY-RUN]", style="bold yellow")

	# Left pane: scrolled interactive menu centered around cursor
	menu_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
	menu_table.add_column("Row", ratio=1)
	menu_table.add_column("Val", justify="right")

	window_size = 24
	start = max(0, min(state.cursor - window_size // 2, max(0, len(rows) - window_size)))
	end = min(len(rows), start + window_size)

	section_titles = {
		"mode": "BUILD MODE",
		"comp": "COMPONENTS",
		"os": "TARGET OS",
		"arch": "ARCHITECTURES",
		"custom": "CUSTOM TRIPLES",
		"opt": "OPTIONS & CROSS-COMPILE",
		"action": "ACTIONS",
	}
	last_kind: Optional[str] = None

	for idx in range(start, end):
		row = rows[idx]
		if row.kind != last_kind:
			menu_table.add_row(Text(f"── {section_titles[row.kind]}", style="bold dim cyan"), Text(""))
			last_kind = row.kind

		is_focus = idx == state.cursor
		prefix = "▸ " if is_focus else "  "
		label_style = "bold black on bright_cyan" if is_focus else "white"
		val_text = _format_row_value(row, cfg)

		if row.kind in ("comp", "os", "arch"):
			active = (
				(row.kind == "comp" and row.key in cfg.components)
				or (row.kind == "os" and row.key in cfg.oses)
				or (row.kind == "arch" and row.key in cfg.arches)
			)
			box_icon = "[●]" if active else "[ ]"
			row_text = Text(f"{prefix}{box_icon} {row.label}", style=label_style if is_focus else ("bold green" if active else "dim white"))
		elif row.kind == "action":
			icon = {"build": "▶", "tools": "⚙", "quit": "✖"}.get(row.key, "•")
			act_style = "bold black on bright_green" if (is_focus and row.key == "build") else (
				"bold black on bright_red" if (is_focus and row.key == "quit") else label_style
			)
			row_text = Text(f"{prefix}{icon} {row.label}", style=act_style)
		else:
			row_text = Text(f"{prefix}{row.label}", style=label_style)

		menu_table.add_row(row_text, Text(val_text, style="bold yellow" if is_focus else "cyan"))

	left_panel = Panel(
		menu_table,
		title="[bold cyan]Configuration[/bold cyan]",
		subtitle=f"[dim]{focused.sub_hint}[/dim]",
		border_style="cyan",
		box=box.ROUNDED,
	)

	# Right top pane: Resolved Target Matrix
	target_table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
	target_table.add_column("Target Triple", style="bold white", no_wrap=True)
	target_table.add_column("OS", style="cyan", no_wrap=True)
	target_table.add_column("Driver", style="green", no_wrap=True)

	for triple in targets[:10]:
		t_os = target_os_from_triple(triple)
		driver = " ".join(resolve_build_tool(triple, cfg.cross_backend, host_os=state.host_os, has_cross=state.has_cross, has_zigbuild=state.has_zigbuild))
		target_table.add_row(triple, t_os, driver)
	if len(targets) > 10:
		target_table.add_row(f"... +{len(targets) - 10} more", "", "")
	if not targets:
		target_table.add_row("[red]No valid targets[/red]", "-", "-")

	matrix_subtitle = f"[yellow]Skipped unsupported: {', '.join(skipped[:4])}[/yellow]" if skipped else f"[dim]{len(targets)} target(s)[/dim]"
	target_panel = Panel(
		target_table,
		title="[bold green]Target Matrix[/bold green]",
		subtitle=matrix_subtitle,
		border_style="green",
		box=box.ROUNDED,
	)

	# Right bottom pane: Live Command Preview & Cache Info
	preview_lines = Text()
	preview_lines.append("Cache: ", style="bold cyan")
	preview_lines.append(f"{state.cargo_target_dir}\n", style="dim")
	preview_lines.append("Tools: ", style="bold cyan")
	preview_lines.append(f"{', '.join(state.detected_tools) or 'none detected'}\n\n", style="green")

	if targets:
		sample_target = targets[0]
		selected_comps = list(SDK_COMPONENTS) if cfg.mode == "sdk" and not cfg.components else cfg.components
		for comp in selected_comps[:4]:
			_, cmd = build_cargo_command(
				comp,
				sample_target,
				cfg,
				host_os=state.host_os,
				has_cross=state.has_cross,
				has_zigbuild=state.has_zigbuild,
			)
			preview_lines.append(f"$ {' '.join(cmd)}\n", style="white")
		if len(selected_comps) > 4:
			preview_lines.append(f"... +{len(selected_comps) - 4} more component command(s)\n", style="dim")

	preview_panel = Panel(
		preview_lines,
		title="[bold magenta]Command Preview[/bold magenta]",
		border_style="magenta",
		box=box.ROUNDED,
	)

	# Footer keybindings bar
	footer_text = Text()
	footer_text.append(" ↑↓ / W S ", style="bold black on white")
	footer_text.append(" Move  ", style="dim")
	footer_text.append(" ←→ / A D ", style="bold black on white")
	footer_text.append(" Cycle/Jump  ", style="dim")
	footer_text.append(" E ", style="bold black on bright_green")
	footer_text.append(" Interact  ", style="dim")
	footer_text.append(" Q ", style="bold black on bright_yellow")
	footer_text.append(" Sub-Action  ", style="dim")
	footer_text.append(" B ", style="bold black on bright_cyan")
	footer_text.append(" Build  ", style="dim")
	footer_text.append(" Esc ", style="bold black on red")
	footer_text.append(" Quit", style="dim")
	footer_text.append(f"   │ {state.status_msg}", style="italic cyan")

	root = Layout()
	root.split_column(
		Layout(Panel(header_text, box=box.ROUNDED, border_style="bright_cyan"), size=3),
		Layout(name="body"),
		Layout(Panel(footer_text, box=box.ROUNDED, border_style="dim"), size=3),
	)
	root["body"].split_row(
		Layout(left_panel, ratio=5),
		Layout(name="right", ratio=6),
	)
	root["right"].split_column(
		Layout(target_panel, ratio=5),
		Layout(preview_panel, ratio=4),
	)
	return root


def _format_row_value(row: MenuRow, cfg: BuildConfig) -> str:
	if row.kind == "mode":
		return cfg.mode.upper()
	if row.kind == "custom":
		return ", ".join(cfg.custom_targets) if cfg.custom_targets else "(none)"
	if row.kind == "opt":
		mapping = {
			"profile": cfg.profile,
			"musl": "ON" if cfg.musl else "off",
			"cross_backend": cfg.cross_backend,
			"build_all": "ON" if cfg.build_all else "off",
			"features": cfg.features or "(default)",
			"jobs": str(cfg.jobs) if cfg.jobs else "auto",
			"deny_admin": "ON" if cfg.deny_admin_setup else "off",
			"no_embed": "ON" if cfg.no_embed else "off",
			"dev_content": "ON" if cfg.dev_content else "off",
			"dry_run": "ON" if cfg.dry_run else "off",
		}
		return mapping.get(row.key, "")
	return ""


def launch_tui(initial: BuildConfig) -> Optional[BuildConfig]:
	"""Runs the interactive Rich TUI with Arrows/WASD + E + Q keybindings."""
	try:
		from rich.console import Console
		from rich.live import Live
	except ImportError:
		print("NOTE: Install `rich` (`pip install rich`) for the full interactive TUI.", file=sys.stderr)
		return _run_minimal_prompt_fallback(initial)

	console = Console()
	state = TuiState(initial)

	with Live(
		render_rich_dashboard(state),
		console=console,
		screen=True,
		refresh_per_second=TUI_FPS,
		auto_refresh=False,
	) as live:
		with KeyPoller() as poller:
			dirty = True
			while True:
				if dirty:
					live.update(render_rich_dashboard(state), refresh=True)
					dirty = False

				keys = poller.poll_all(timeout=TUI_TICK_SECONDS)
				if not keys:
					continue

				for key in keys:
					outcome = state.handle_key(key)
					dirty = True
					if outcome == "quit":
						return None
					if outcome == "build":
						return state.cfg
					if outcome == "tools":
						poller.__exit__(None, None, None)
						live.stop()
						run_check_tools()
						input("\nPress Enter to return to RBE Build Studio...")
						live.start()
						poller.__enter__()
						break
					if outcome == "prompt":
						poller.__exit__(None, None, None)
						live.stop()
						if state.pending_prompt == "custom_target":
							raw = input("\nAdd custom Rust target triple(s) (comma-separated, e.g. x86_64-pc-windows-gnu): ").strip()
							if raw:
								for part in raw.split(","):
									triple = part.strip()
									if triple and triple not in state.cfg.custom_targets:
										state.cfg.custom_targets.append(triple)
								state.status_msg = f"Custom targets: {', '.join(state.cfg.custom_targets)}"
						elif state.pending_prompt == "features":
							raw = input("\nCargo features (comma-separated, blank to clear): ").strip()
							state.cfg.features = raw
							state.status_msg = f"Features -> {raw or '(default)'}"
						state.pending_prompt = None
						live.start()
						poller.__enter__()
						break


def _run_minimal_prompt_fallback(initial: BuildConfig) -> Optional[BuildConfig]:
	"""Ultra-minimal fallback only if `rich` is not installed."""
	state = TuiState(initial)
	print("RBE Build Studio (install `rich` for the full keyboard TUI)")
	print(f"Defaults: mode={state.cfg.mode}, components={state.cfg.components}, targets={resolve_targets(state.cfg)}")
	ans = input("Proceed with default build? [y/N]: ").strip().lower()
	return state.cfg if ans == "y" else None


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------

def parse_args(argv: Sequence[str]) -> Tuple[BuildConfig, bool]:
	"""Parses CLI arguments into `(BuildConfig, should_launch_tui)`."""
	expanded: List[str] = []
	for token in argv:
		if token.startswith("--only-"):
			expanded.extend(["--only", token[len("--only-"):]])
		else:
			expanded.append(token)

	parser = argparse.ArgumentParser(
		prog="build.py",
		description="Cross-platform RBE Build Studio (Rich TUI + CLI) for components, SDK bundles, and multi-OS/arch targets.",
	)
	parser.add_argument("--tui", action="store_true", help="Launch the interactive Rich TUI.")
	parser.add_argument("--build-sdk", action="store_true", help="Build the RBE SDK bundle (sdk-backend, rpx, rel-lsp, bindings).")
	parser.add_argument("--full-release", action="store_true", help="Run the full secure release pipeline.")
	parser.add_argument("--check", action="store_true", help="Run `cargo check` instead of `cargo build`.")
	parser.add_argument("--test", action="store_true", help="Run `cargo test` for selected components.")
	parser.add_argument("--only", action="append", default=[], metavar="COMPONENT", help=f"Component ({', '.join(COMPONENT_ORDER)}). Repeatable.")
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
	parser.add_argument("--arch-riscv64", action="store_true", help="Target riscv64.")
	parser.add_argument("--arch-wasm32", action="store_true", help="Target wasm32.")
	parser.add_argument("--target", action="append", default=[], metavar="TRIPLE", help="Explicit Rust target triple (repeatable).")
	parser.add_argument("--cross-backend", choices=CROSS_BACKENDS, default="auto", help="Cross-compilation driver (auto/cargo/cross/zigbuild/xwin).")
	parser.add_argument("--features", default="", help="Comma-separated Cargo features.")
	parser.add_argument("-j", "--jobs", type=int, default=None, help="Parallel Cargo build jobs.")
	parser.add_argument("--musl", action="store_true", help="Use Linux musl targets (or bare wasm32-unknown-unknown).")
	parser.add_argument("--debug", action="store_true", help="Build debug profile instead of release.")
	parser.add_argument("--deny-admin-setup", action="store_true", help="Skip compile-time admin password prompt.")
	parser.add_argument("--no-embed", action="store_true", help="Full release: do not embed container-bin.")
	parser.add_argument("--dev-content", action="store_true", help="Full release: bundle dev api/module files.")
	parser.add_argument("--check-tools", action="store_true", help="Verify Rust, xwin, LLVM, and C prerequisites and exit.")
	parser.add_argument("--dry-run", action="store_true", help="Print resolved build commands without executing Cargo.")

	ns = parser.parse_args(expanded)

	oses = list(ns.os)
	for flag, name in ((ns.build_linux, "linux"), (ns.build_windows, "windows"), (ns.build_macos, "macos")):
		if flag and name not in oses:
			oses.append(name)

	arches = list(ns.arch)
	for flag, name in (
		(ns.arch_x64, "x64"),
		(ns.arch_arm64, "arm64"),
		(ns.arch_x86, "x86"),
		(ns.arch_armv7, "armv7"),
		(ns.arch_riscv64, "riscv64"),
		(ns.arch_wasm32, "wasm32"),
	):
		if flag and name not in arches:
			arches.append(name)

	components = [normalize_component(c) for c in ns.only]
	if ns.build_sdk:
		mode = "sdk"
		if not components:
			components = list(SDK_COMPONENTS)
	elif ns.full_release:
		mode = "full"
	elif ns.check:
		mode = "check"
		if not components:
			components = ["backend"]
	elif ns.test:
		mode = "test"
		if not components:
			components = ["rel-lsp"]
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
		cross_backend=ns.cross_backend,
		features=ns.features,
		jobs=ns.jobs,
		deny_admin_setup=ns.deny_admin_setup,
		no_embed=ns.no_embed,
		dev_content=ns.dev_content,
		check_tools=ns.check_tools,
		dry_run=ns.dry_run,
	)

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