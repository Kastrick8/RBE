# RBE — Agent Instructions (`AGENTS.md`)

## 1. Repository Map

- `engine/crates/route-engine/` — REL language frontend (`lexer.rs`, `parser.rs`, `analyzer.rs`), `relc`, optimizer, WASM/native lowering, `quickdb`, `field_manager`.
- `engine/crates/rel-lsp/` — REL Language Server (`rel-lsp --stdio`) for live diagnostics, autocomplete, hover, and go-to-definition.
- `engine/crates/backend/` — Main RBE server, `package_links`, `deployment_worker`, `service_mother`, `hot_reload`, CLI entrypoints.
- `engine/crates/core/` — Shared networking (`network_broker`, `dns_broker`, `public_https`), `metrics`, `container_client`, `library_host`.
- `engine/crates/api/` — Admin/health routes and embedded web dashboard.
- `engine/crates/sdk-backend/` & `engine/crates/sdk-package/` — SDK toolchain activation, packaging, and local authoring.
- `engine/crates/rpx/` — Package/application CLI and registry client (including `rpx lsp` and `rpx dev`).
- `engine/crates/cloud-node/` — Peer sync, provider sync, and local sync node.
- `engine/crates/service-runtime/`, `security/`, `supervisor/`, `video-manager/`, `config/`, `logging/` — Core runtime subsystems.
- `container-runtime/` — Sandboxed execution workspace (`landlock`, `ipc-protocol`, `resource-limits`).
- `atomic-io/` — Atomic file I/O crate.
- `editors/vscode-rel/` — Minimal VS Code extension for REL wired to `rpx lsp`.
- `doc/` — **THE ONLY canonical documentation directory.**

## 2. Documentation Rules (Strict)

1. **Dual-document every change:** Whenever you add or change a feature, REL syntax/builtin, CLI command, error code, or config option, you MUST document it in **both**:
	- **In source:** Module doc comments (`//!`), public item doc comments (`///`), and clear inline comments on non-trivial logic.
	- **In `doc/`:** Update the matching guide under `doc/` (and `doc/error-codes/` + `catalog.json` if adding error codes).
2. **Never use `docs/`:** `doc/` is the sole documentation folder. Never create `docs/` or write new files into it. If legacy `docs/` files still exist, audit and merge any accurate information into `doc/` before deleting the legacy file—never discard accurate info without acknowledging and migrating it.
3. **Keep `doc/README.md` in sync:** Any new document added under `doc/` must be linked from `doc/README.md`.

## 3. Code Style & Quality

- **Indentation:** Use literal TAB characters (`\t`) for indentation in all code files you create or edit. Never mix tabs and spaces.
- **Modularity (DRY):** Keep functions small, focused, and readable with descriptive names. Avoid dumping new subsystems into already-large files (e.g., `backend/src/main.rs` or `route-engine/src/relc.rs`); extract dedicated modules.
- **Onboarding comments:** Comment *why* non-obvious invariants, lifetimes, IPC frames, or security checks exist so a new contributor can understand the module quickly.
- **Error codes:** New user-facing errors must use the structured error-code book (`error_code_book_core.rs`) rather than ad-hoc strings.

## 4. Build, Test & Low-Memory QOL

- Prefer targeted crate builds and tests over full-workspace rebuilds:
	- `cargo check -p route-engine`
	- `cargo test -p route-engine -- <test_name>`
- Use the repo's cross-platform `python3 build.py` (interactive TUI or CLI flags, e.g. `python3 build.py --build-sdk --build-linux --arch-x64`) or legacy `build-select.sh` / `build.sh` / `build.ps1` scripts rather than inventing ad-hoc build commands.
- Always run `cargo test` on the modified crate(s) before finishing a task.

## 5. Repo Hygiene (No Junk)

- Never commit empty placeholder files (e.g., `_ignore`), temporary session notes (`*-resume*.md`), or runtime state files under `data/`.
- Never leave one-shot migration/benchmark workflows in `.github/workflows/` after they have served their purpose.
- Do not duplicate binaries or config templates without updating `.gitignore` and CI manifests.
