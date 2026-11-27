//! Hover documentation and Go-to-Definition navigation for REL files.
//!
//! Resolves:
//! - Host builtins (`workspace`, `script`, `archive`) and their member functions.
//! - Local declarations (`fn`, `class`, `const`, `let`) within the open document.
//! - Module/service/field imports (`:import[foo]` or `:import[foo from bar]`) to their
//!   corresponding `.module`, `.service`, or `.field` source files on disk.

use route_engine::rel_host_builtins::{
	allowed_for_role, ARCHIVE_BUILTIN, ARCHIVE_FUNCTIONS, SCRIPT_BUILTIN, SCRIPT_FUNCTIONS,
	WORKSPACE_BUILTIN, WORKSPACE_FUNCTIONS,
};
use route_engine::RelSourceKind;
use std::path::Path;
use tower_lsp::lsp_types::{
	Hover, HoverContents, Location, MarkupContent, MarkupKind, Position, Range, Url,
};

/// Returns hover documentation for the identifier under `position`, if recognized.
pub fn hover_at(source: &str, position: Position, kind: RelSourceKind) -> Option<Hover> {
	let word = word_at_position(source, position)?;
	let markdown = match word.as_str() {
		WORKSPACE_BUILTIN => format!(
			"**`workspace`** (Host Builtin)\n\nExecution-scoped workspace (`??/`) orchestration.\n\nFunctions: `{}`\n\nAllowed in `{kind:?}`: `{}`",
			WORKSPACE_FUNCTIONS.join("`, `"),
			allowed_for_role(WORKSPACE_BUILTIN, kind)
		),
		SCRIPT_BUILTIN => format!(
			"**`script`** (Host Builtin)\n\nSandboxed managed script runner.\n\nFunctions: `{}`\n\nAllowed in `{kind:?}`: `{}`",
			SCRIPT_FUNCTIONS.join("`, `"),
			allowed_for_role(SCRIPT_BUILTIN, kind)
		),
		ARCHIVE_BUILTIN => format!(
			"**`archive`** (Host Builtin)\n\nBounded host-side archive operations.\n\nFunctions: `{}`\n\nAllowed in `{kind:?}`: `{}`",
			ARCHIVE_FUNCTIONS.join("`, `"),
			allowed_for_role(ARCHIVE_BUILTIN, kind)
		),
		"quickDB" => "**`quickDB`** (Service Capability)\n\nIn-memory probabilistic Bloom/counting/scalable filter capability. Available exclusively in `.service` files.".to_string(),
		_ => {
			// Fall back to local declaration preview if the symbol is defined in this file.
			let decl_line = find_local_declaration_line(source, &word)?;
			format!("```rel\n{}\n```", decl_line.trim())
		}
	};

	Some(Hover {
		contents: HoverContents::Markup(MarkupContent {
			kind: MarkupKind::Markdown,
			value: markdown,
		}),
		range: None,
	})
}

/// Resolves Go-to-Definition for local symbols, module/service imports, and `.field` schemas.
pub fn goto_definition(
	uri: &Url,
	source: &str,
	position: Position,
	workspace_root: Option<&Path>,
) -> Option<Location> {
	let word = word_at_position(source, position)?;

	// 1. Check for a local declaration in the current document.
	if let Some((line_idx, col_idx)) = find_local_declaration_span(source, &word) {
		let start = Position::new(line_idx as u32, col_idx as u32);
		let end = Position::new(line_idx as u32, (col_idx + word.len()) as u32);
		return Some(Location {
			uri: uri.clone(),
			range: Range { start, end },
		});
	}

	// 2. Check workspace files for matching `.module`, `.service`, or `.field` definitions.
	let root = workspace_root?;
	let candidates = [
		root.join("module").join(format!("{word}.module")),
		root.join("modules").join(format!("{word}.module")),
		root.join("service").join(format!("{word}.service")),
		root.join("services").join(format!("{word}.service")),
		root.join("api").join(format!("{word}.field")),
		root.join("fields").join(format!("{word}.field")),
		root.join(format!("{word}.module")),
		root.join(format!("{word}.service")),
		root.join(format!("{word}.field")),
	];

	for path in candidates {
		if path.is_file() {
			if let Ok(target_uri) = Url::from_file_path(&path) {
				return Some(Location {
					uri: target_uri,
					range: Range {
						start: Position::new(0, 0),
						end: Position::new(0, 0),
					},
				});
			}
		}
	}

	None
}

/// Extracts the identifier token under `position`.
fn word_at_position(source: &str, position: Position) -> Option<String> {
	let line = source.lines().nth(position.line as usize)?;
	let bytes = line.as_bytes();
	if bytes.is_empty() {
		return None;
	}
	let mut idx = (position.character as usize).min(bytes.len().saturating_sub(1));
	if !is_ident_byte(bytes[idx]) && idx > 0 && is_ident_byte(bytes[idx - 1]) {
		idx -= 1;
	}
	if !is_ident_byte(bytes[idx]) {
		return None;
	}
	let mut start = idx;
	while start > 0 && is_ident_byte(bytes[start - 1]) {
		start -= 1;
	}
	let mut end = idx + 1;
	while end < bytes.len() && is_ident_byte(bytes[end]) {
		end += 1;
	}
	Some(line[start..end].to_string())
}

fn is_ident_byte(b: u8) -> bool {
	b.is_ascii_alphanumeric() || b == b'_'
}

fn find_local_declaration_line<'a>(source: &'a str, symbol: &str) -> Option<&'a str> {
	let (line_idx, _) = find_local_declaration_span(source, symbol)?;
	source.lines().nth(line_idx)
}

fn find_local_declaration_span(source: &str, symbol: &str) -> Option<(usize, usize)> {
	for (line_idx, line) in source.lines().enumerate() {
		let trimmed = line.trim_start();
		let leading = line.len() - trimmed.len();
		for prefix in ["fn ", "export fn ", "class ", "const ", "let "] {
			if let Some(rest) = trimmed.strip_prefix(prefix) {
				let ident: String = rest
					.chars()
					.take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
					.collect();
				if ident == symbol {
					return Some((line_idx, leading + prefix.len()));
				}
			}
		}
	}
	None
}

#[cfg(test)]
mod tests {
	use super::*;

	#[test]
	fn hover_explains_host_builtin() {
		let src = "GET {\n\tworkspace.temp();\n}\n";
		let hover = hover_at(src, Position::new(1, 3), RelSourceKind::Route).unwrap();
		match hover.contents {
			HoverContents::Markup(m) => assert!(m.value.contains("workspace")),
			_ => panic!("expected markdown hover"),
		}
	}

	#[test]
	fn goto_definition_finds_local_function() {
		let uri = Url::parse("file:///project/api/hello.route").unwrap();
		let src = "fn helper() {\n\treturn 1;\n}\nGET {\n\treturn helper();\n}\n";
		let loc = goto_definition(&uri, src, Position::new(4, 10), None).unwrap();
		assert_eq!(loc.range.start.line, 0);
	}
}
