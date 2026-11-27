# `rel-lsp` — REL Language Server

`rel-lsp` is RBE's first-class Language Server Protocol (LSP) server for the Runtime Engine Language (`.route`, `.module`, `.service`, `.field`, and `server.server`). It lives in `engine/crates/rel-lsp` as a workspace member, reuses `route-engine`'s compiler frontend and `sdk-package`'s manifest inspector directly, and communicates over standard `--stdio` transport so VS Code, Neovim, Helix, and Zed work with zero custom transport glue.

## Unified SDK Entrypoint (`rpx lsp`)

Instead of pointing editors directly at a global binary, editor integrations invoke:

```text
rpx lsp
```

`rpx lsp` resolves the `rel-lsp` binary for the active project SDK toolchain in the following order:

1. `RBE_REL_LSP_BIN` environment override (when explicitly set).
2. Project-local SDK toolchain binary at `<project>/.rbe/bin/rel-lsp` (or `rel-lsp.exe` on Windows), installed and registered by `sdk-backend` (`toolchain_install.rs` and `activation.rs`).
3. Sibling `rel-lsp` binary located next to the running `rpx` executable.
4. `rel-lsp` on `PATH` as a last fallback.

This guarantees that every editor session automatically runs the exact `rel-lsp` version matching the project's activated SDK.

## Capabilities

- **Live in-memory diagnostics (`did_open` / `did_change`):** Runs `route-engine`'s `Lexer -> Parser -> Analyzer` pipeline directly on unsaved buffer contents and publishes LSP diagnostics tagged with RBE's error-code book identifiers (`REL1001`, `REL1101`, `REL1304`, `E2001`, `E3000`; see [`error-codes/rel.md`](error-codes/rel.md) and [`error-codes/route-error-book.md`](error-codes/route-error-book.md)).
- **SDK-aware autocomplete:** Combines:
	- Local symbols (`fn`, `export fn`, `class`, `const`, `let`) declared in the current buffer.
	- Host builtins (`workspace`, `script`, `archive`) and their member functions (`workspace.*`, `script.*`, `archive.*`), filtered by `rel_host_builtins::allowed_for_role` so `.field` files never receive host-builtin suggestions.
	- Core REL modules (`net`, `json`, `time`, `log`, `private`, `storage`) and Service-only `quickDB`.
	- Installed and workspace-local SDK packages discovered via `sdk-package` (`package.rbe.toml`), inserting `:import[<component> from <package>]`.
- **Hover & Go-to-Definition:** Resolves host builtins, local declarations, and workspace `.module`, `.service`, and `.field` files to their source locations.

## Editor Configuration

### VS Code

Install or load the bundled extension in `editors/vscode-rel/`, which registers `.route`, `.module`, `.service`, `.field`, and `server.server` files and spawns `rpx lsp` automatically.

### Neovim (`nvim-lspconfig`)

```lua
vim.lsp.start({
	name = "rel-lsp",
	cmd = { "rpx", "lsp" },
	root_dir = vim.fs.root(0, { "server.server", "package.rbe.yaml", "package.rbe.toml", ".git" }),
})
```

### Helix (`languages.toml`)

```toml
[language-server.rel-lsp]
command = "rpx"
args = ["lsp"]
```

### Zed (`settings.json`)

```json
{
	"lsp": {
		"rel-lsp": {
			"binary": {
				"path": "rpx",
				"arguments": ["lsp"]
			}
		}
	}
}
```
