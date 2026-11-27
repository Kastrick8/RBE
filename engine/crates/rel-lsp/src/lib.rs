//! REL Language Server (`rel-lsp`) library entrypoint.
//!
//! Wires RBE's existing `route-engine` frontend (`lexer`, `parser`, `analyzer`,
//! `rel_host_builtins`, `field_manager`) and `sdk-package` metadata into a
//! standard `tower-lsp` server over stdio so VS Code, Neovim, Helix, and Zed
//! all get live diagnostics, SDK-aware completion, hover, and go-to-definition.

pub mod completion;
pub mod diagnostics;
pub mod navigation;
pub mod server;

use route_engine::RelSourceKind;
use tower_lsp::lsp_types::Url;

/// Infers the REL source role from an LSP document URI.
///
/// Defaults to `RelSourceKind::Route` when an untitled or unknown buffer is opened
/// so authors still receive core lexer/parser diagnostics while prototyping.
pub fn source_kind_for_uri(uri: &Url) -> RelSourceKind {
	let path = uri.path();
	if path.ends_with("server.server") || path.ends_with(".server") {
		RelSourceKind::Server
	} else if path.ends_with(".module") {
		RelSourceKind::Module
	} else if path.ends_with(".service") {
		RelSourceKind::Service
	} else if path.ends_with(".field") {
		RelSourceKind::Field
	} else {
		RelSourceKind::Route
	}
}
