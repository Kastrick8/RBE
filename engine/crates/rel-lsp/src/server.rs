//! `tower-lsp` LanguageServer backend for REL (`rel-lsp`).
//!
//! Maintains an in-memory map of open documents, republishes diagnostics on every
//! `did_open` / `did_change` notification, and serves completion, hover, and
//! go-to-definition requests over stdio.

use crate::completion::complete_at;
use crate::diagnostics::diagnose_source;
use crate::navigation::{goto_definition, hover_at};
use crate::source_kind_for_uri;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::RwLock;
use tower_lsp::jsonrpc::Result;
use tower_lsp::lsp_types::*;
use tower_lsp::{Client, LanguageServer};

/// Stateful LSP backend shared across incoming JSON-RPC requests.
pub struct RelLanguageServer {
	client: Client,
	documents: RwLock<HashMap<Url, String>>,
	workspace_root: RwLock<Option<PathBuf>>,
}

impl RelLanguageServer {
	/// Creates a new `RelLanguageServer` bound to the given `tower-lsp` client handle.
	pub fn new(client: Client) -> Self {
		Self {
			client,
			documents: RwLock::new(HashMap::new()),
			workspace_root: RwLock::new(None),
		}
	}

	/// Updates the in-memory document snapshot and pushes fresh diagnostics to the editor.
	async fn update_and_publish(&self, uri: Url, text: String, version: Option<i32>) {
		let kind = source_kind_for_uri(&uri);
		let diagnostics = diagnose_source(&text, kind);
		if let Ok(mut docs) = self.documents.write() {
			docs.insert(uri.clone(), text);
		}
		self.client
			.publish_diagnostics(uri, diagnostics, version)
			.await;
	}

	fn get_document(&self, uri: &Url) -> Option<String> {
		self.documents
			.read()
			.ok()
			.and_then(|docs| docs.get(uri).cloned())
	}

	fn get_workspace_root(&self) -> Option<PathBuf> {
		self.workspace_root
			.read()
			.ok()
			.and_then(|root| root.clone())
	}
}

#[tower_lsp::async_trait]
impl LanguageServer for RelLanguageServer {
	async fn initialize(&self, params: InitializeParams) -> Result<InitializeResult> {
		// Record the workspace root so completion and go-to-definition can discover SDK packages and modules.
		let root = params
			.root_uri
			.as_ref()
			.and_then(|u| u.to_file_path().ok())
			.or_else(|| {
				params
					.workspace_folders
					.as_ref()
					.and_then(|folders| folders.first())
					.and_then(|folder| folder.uri.to_file_path().ok())
			});
		if let Ok(mut slot) = self.workspace_root.write() {
			*slot = root;
		}

		Ok(InitializeResult {
			server_info: Some(ServerInfo {
				name: "rel-lsp".to_string(),
				version: Some(env!("CARGO_PKG_VERSION").to_string()),
			}),
			capabilities: ServerCapabilities {
				text_document_sync: Some(TextDocumentSyncCapability::Kind(
					TextDocumentSyncKind::FULL,
				)),
				completion_provider: Some(CompletionOptions {
					trigger_characters: Some(vec![".".to_string(), "[".to_string()]),
					..Default::default()
				}),
				hover_provider: Some(HoverProviderCapability::Simple(true)),
				definition_provider: Some(OneOf::Left(true)),
				..Default::default()
			},
		})
	}

	async fn initialized(&self, _: InitializedParams) {
		self.client
			.log_message(MessageType::INFO, "rel-lsp initialized and ready")
			.await;
	}

	async fn shutdown(&self) -> Result<()> {
		Ok(())
	}

	async fn did_open(&self, params: DidOpenTextDocumentParams) {
		self.update_and_publish(
			params.text_document.uri,
			params.text_document.text,
			Some(params.text_document.version),
		)
		.await;
	}

	async fn did_change(&self, params: DidChangeTextDocumentParams) {
		// With TextDocumentSyncKind::FULL, the final change carries the complete buffer text.
		if let Some(change) = params.content_changes.into_iter().last() {
			self.update_and_publish(
				params.text_document.uri,
				change.text,
				Some(params.text_document.version),
			)
			.await;
		}
	}

	async fn did_close(&self, params: DidCloseTextDocumentParams) {
		if let Ok(mut docs) = self.documents.write() {
			docs.remove(&params.text_document.uri);
		}
		self.client
			.publish_diagnostics(params.text_document.uri, Vec::new(), None)
			.await;
	}

	async fn completion(&self, params: CompletionParams) -> Result<Option<CompletionResponse>> {
		let uri = &params.text_document_position.text_document.uri;
		let Some(source) = self.get_document(uri) else {
			return Ok(None);
		};
		let kind = source_kind_for_uri(uri);
		let root = self.get_workspace_root();
		let items = complete_at(
			&source,
			params.text_document_position.position,
			kind,
			root.as_deref(),
		);
		Ok(Some(CompletionResponse::Array(items)))
	}

	async fn hover(&self, params: HoverParams) -> Result<Option<Hover>> {
		let uri = &params.text_document_position_params.text_document.uri;
		let Some(source) = self.get_document(uri) else {
			return Ok(None);
		};
		let kind = source_kind_for_uri(uri);
		Ok(hover_at(
			&source,
			params.text_document_position_params.position,
			kind,
		))
	}

	async fn goto_definition(
		&self,
		params: GotoDefinitionParams,
	) -> Result<Option<GotoDefinitionResponse>> {
		let uri = &params.text_document_position_params.text_document.uri;
		let Some(source) = self.get_document(uri) else {
			return Ok(None);
		};
		let root = self.get_workspace_root();
		let location = goto_definition(
			uri,
			&source,
			params.text_document_position_params.position,
			root.as_deref(),
		);
		Ok(location.map(GotoDefinitionResponse::Scalar))
	}
}
