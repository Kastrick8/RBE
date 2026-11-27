//! `rel-lsp` CLI binary entrypoint.
//!
//! Runs the REL Language Server over stdio (`rel-lsp --stdio`) so any LSP-capable
//! editor (VS Code, Neovim, Helix, Zed) or `rpx lsp` can spawn it with zero
//! custom transport configuration.

use rel_lsp::server::RelLanguageServer;
use tower_lsp::{LspService, Server};

#[tokio::main]
async fn main() {
	let args: Vec<String> = std::env::args().skip(1).collect();
	if args.iter().any(|arg| arg == "--version" || arg == "-V") {
		println!("rel-lsp {}", env!("CARGO_PKG_VERSION"));
		return;
	}
	if args.iter().any(|arg| arg == "--help" || arg == "-h") {
		println!(
			"rel-lsp {} — RBE Runtime Engine Language Server\n\n\
			USAGE:\n\
			\trel-lsp --stdio\n\
			\trel-lsp --version\n\n\
			Editors should normally invoke `rpx lsp`, which resolves and runs the\n\
			active project SDK toolchain's `rel-lsp` binary over stdio.",
			env!("CARGO_PKG_VERSION")
		);
		return;
	}

	// Default to stdio transport (with or without explicit `--stdio`) for maximum editor compatibility.
	let stdin = tokio::io::stdin();
	let stdout = tokio::io::stdout();
	let (service, socket) = LspService::new(RelLanguageServer::new);
	Server::new(stdin, stdout, socket).serve(service).await;
}
