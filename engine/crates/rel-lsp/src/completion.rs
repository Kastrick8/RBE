//! SDK-aware autocomplete for REL source files.
//!
//! Combines three sources of completion items:
//! 1. Local symbols declared in the active document (`fn`, `class`, `const`, `let`, `:import[...]`).
//! 2. Role-permitted host builtins (`workspace`, `script`, `archive`) and their member functions
//!    from `route_engine::rel_host_builtins`, plus core REL runtime modules.
//! 3. Installed or workspace-local SDK packages discovered via `sdk_package::check_package`.

use route_engine::rel_host_builtins::{
	allowed_for_role, ARCHIVE_BUILTIN, ARCHIVE_FUNCTIONS, SCRIPT_BUILTIN, SCRIPT_FUNCTIONS,
	WORKSPACE_BUILTIN, WORKSPACE_FUNCTIONS,
};
use route_engine::RelSourceKind;
use sdk_package::check_package;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use tower_lsp::lsp_types::{
	CompletionItem, CompletionItemKind, Documentation, MarkupContent, MarkupKind, Position,
};

/// Built-in non-host REL modules available across route/module/service files.
const CORE_REL_MODULES: &[(&str, &str)] = &[
	("net", "Bounded HTTP/network client helpers (`net.get`, `net.post`, `net.fetch`)."),
	("json", "JSON serialization and parsing helpers (`json.parse`, `json.stringify`)."),
	("time", "Clock and timestamp utilities (`time.now`, `time.iso`)."),
	("log", "Structured runtime logging (`log.info`, `log.warn`, `log.error`)."),
	("private", "Typed Runtime ENV and private configuration accessors."),
	("storage", "Environment Storage helpers (`storage.read`, `storage.write`)."),
];

/// Computes context-aware completion items for the cursor position in `source`.
pub fn complete_at(
	source: &str,
	position: Position,
	kind: RelSourceKind,
	workspace_root: Option<&Path>,
) -> Vec<CompletionItem> {
	let line_prefix = line_prefix_at(source, position);

	// Member completion after `<module>.` (e.g., `workspace.`, `script.`, `archive.`).
	if let Some(receiver) = trailing_member_receiver(&line_prefix) {
		return complete_member_functions(receiver, kind);
	}

	let mut items = Vec::new();
	let mut seen = BTreeSet::new();

	// 1. Local symbols declared in the current buffer.
	for sym in extract_local_symbols(source) {
		if seen.insert(sym.clone()) {
			items.push(CompletionItem {
				label: sym,
				kind: Some(CompletionItemKind::VARIABLE),
				detail: Some("Local REL symbol".to_string()),
				..Default::default()
			});
		}
	}

	// 2. Host builtins gated by the active REL source role (never offered in `.field`).
	for (module, desc) in [
		(
			WORKSPACE_BUILTIN,
			"Execution-scoped workspace (`??/`) and temporary task orchestration.",
		),
		(
			SCRIPT_BUILTIN,
			"Sandboxed managed script runner (`script.run`, `script.runPyPy`, `script.runRust`).",
		),
		(
			ARCHIVE_BUILTIN,
			"Bounded host-side archive inspection and mutation (`archive.list`, `archive.extract`).",
		),
	] {
		if allowed_for_role(module, kind) && seen.insert(module.to_string()) {
			items.push(CompletionItem {
				label: module.to_string(),
				kind: Some(CompletionItemKind::MODULE),
				detail: Some("RBE Host Builtin".to_string()),
				documentation: Some(markdown_doc(desc)),
				..Default::default()
			});
		}
	}

	// 3. Core REL modules + Service-only `quickDB`.
	if kind != RelSourceKind::Field {
		for (module, desc) in CORE_REL_MODULES {
			if seen.insert((*module).to_string()) {
				items.push(CompletionItem {
					label: (*module).to_string(),
					kind: Some(CompletionItemKind::MODULE),
					detail: Some("REL Standard Module".to_string()),
					documentation: Some(markdown_doc(desc)),
					..Default::default()
				});
			}
		}
	}
	if kind == RelSourceKind::Service && seen.insert("quickDB".to_string()) {
		items.push(CompletionItem {
			label: "quickDB".to_string(),
			kind: Some(CompletionItemKind::MODULE),
			detail: Some("Service-only probabilistic filter capability".to_string()),
			documentation: Some(markdown_doc(
				"In-memory Bloom/counting/scalable filter capability scoped to `.service` files.",
			)),
			..Default::default()
		});
	}

	// 4. Installed / workspace SDK packages discovered via `sdk-package`.
	if let Some(root) = workspace_root {
		for pkg_item in discover_sdk_package_completions(root) {
			if seen.insert(pkg_item.label.clone()) {
				items.push(pkg_item);
			}
		}
	}

	items
}

/// Returns completion items for `<receiver>.<member>` calls on host builtins.
fn complete_member_functions(receiver: &str, kind: RelSourceKind) -> Vec<CompletionItem> {
	if !allowed_for_role(receiver, kind) {
		return Vec::new();
	}
	let members: &[&str] = match receiver {
		WORKSPACE_BUILTIN => WORKSPACE_FUNCTIONS,
		SCRIPT_BUILTIN => SCRIPT_FUNCTIONS,
		ARCHIVE_BUILTIN => ARCHIVE_FUNCTIONS,
		_ => &[],
	};
	members
		.iter()
		.map(|func| CompletionItem {
			label: (*func).to_string(),
			kind: Some(CompletionItemKind::FUNCTION),
			detail: Some(format!("{receiver}.{func}(...)")),
			..Default::default()
		})
		.collect()
}

/// Scans `workspace_root` (and `packages/` / `.rbe/packages/`) for valid `package.rbe.toml`
/// packages using `sdk_package::check_package` and emits `:import[component from package]` items.
fn discover_sdk_package_completions(workspace_root: &Path) -> Vec<CompletionItem> {
	let mut candidate_dirs = vec![workspace_root.to_path_buf()];
	for subdir in ["packages", ".rbe/packages", "sdk/packages"] {
		let dir = workspace_root.join(subdir);
		if let Ok(entries) = fs::read_dir(dir) {
			for entry in entries.flatten() {
				if entry.path().is_dir() {
					candidate_dirs.push(entry.path());
				}
			}
		}
	}

	let mut items = Vec::new();
	for dir in candidate_dirs {
		if !dir.join(sdk_package::PACKAGE_MANIFEST).is_file() {
			continue;
		}
		if let Ok(checked) = check_package(&dir) {
			let pkg_name = &checked.manifest.package.name;
			let pkg_ver = &checked.manifest.package.version;
			let index = checked.index();
			for export in index.exports {
				items.push(CompletionItem {
					label: format!("{pkg_name}::{}", export.name),
					kind: Some(CompletionItemKind::REFERENCE),
					detail: Some(format!("SDK Package {pkg_name} v{pkg_ver}")),
					insert_text: Some(format!(":import[{} from {}]", export.name, pkg_name)),
					documentation: Some(markdown_doc(&format!(
						"SDK export `{}` from package `{pkg_name}` (`{pkg_ver}`).",
						export.name
					))),
					..Default::default()
				});
			}
		}
	}
	items
}

/// Extracts local function, class, constant, and variable names from a REL buffer.
pub(crate) fn extract_local_symbols(source: &str) -> Vec<String> {
	let mut symbols = Vec::new();
	for line in source.lines() {
		let trimmed = line.trim();
		for prefix in ["fn ", "export fn ", "class ", "const ", "let "] {
			if let Some(rest) = trimmed.strip_prefix(prefix) {
				let ident: String = rest
					.chars()
					.take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
					.collect();
				if !ident.is_empty() {
					symbols.push(ident);
				}
			}
		}
	}
	symbols
}

/// Returns the text on `position.line` up to `position.character`.
fn line_prefix_at(source: &str, position: Position) -> String {
	source
		.lines()
		.nth(position.line as usize)
		.map(|line| {
			let end = (position.character as usize).min(line.len());
			line[..end].to_string()
		})
		.unwrap_or_default()
}

/// If `prefix` ends with `<ident>.`, returns `Some(<ident>)`.
fn trailing_member_receiver(prefix: &str) -> Option<&str> {
	let trimmed = prefix.trim_end();
	let before_dot = trimmed.strip_suffix('.')?;
	let start = before_dot
		.rfind(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
		.map(|idx| idx + 1)
		.unwrap_or(0);
	let receiver = &before_dot[start..];
	if receiver.is_empty() {
		None
	} else {
		Some(receiver)
	}
}

fn markdown_doc(content: &str) -> Documentation {
	Documentation::MarkupContent(MarkupContent {
		kind: MarkupKind::Markdown,
		value: content.to_string(),
	})
}

#[cfg(test)]
mod tests {
	use super::*;

	#[test]
	fn offers_workspace_member_completions_in_routes_but_not_fields() {
		let src = "GET {\n\tworkspace.\n}\n";
		let route_items = complete_at(src, Position::new(1, 11), RelSourceKind::Route, None);
		assert!(route_items.iter().any(|i| i.label == "temp"));
		assert!(route_items.iter().any(|i| i.label == "run"));

		let field_items = complete_at(src, Position::new(1, 11), RelSourceKind::Field, None);
		assert!(field_items.is_empty());
	}

	#[test]
	fn offers_quickdb_only_for_service_files() {
		let service_items = complete_at("", Position::new(0, 0), RelSourceKind::Service, None);
		assert!(service_items.iter().any(|i| i.label == "quickDB"));

		let route_items = complete_at("", Position::new(0, 0), RelSourceKind::Route, None);
		assert!(!route_items.iter().any(|i| i.label == "quickDB"));
	}
}
