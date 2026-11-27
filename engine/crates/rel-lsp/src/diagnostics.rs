//! In-memory live diagnostics for REL source files.
//!
//! Runs `route-engine`'s lexer, parser, and analyzer directly on open buffer text
//! and maps findings to LSP `Diagnostic` ranges annotated with RBE's stable
//! error-book codes (`doc/error-codes/rel.md` and `doc/error-codes/route-error-book.md`).

use route_engine::lexer::Lexer;
use route_engine::parser::Parser;
use route_engine::{analyze, parse_server_source, RelSourceKind, Severity};
use tower_lsp::lsp_types::{Diagnostic, DiagnosticSeverity, NumberOrString, Position, Range};

/// Runs lexer -> parser -> analyzer in memory and returns LSP diagnostics.
pub fn diagnose_source(source: &str, kind: RelSourceKind) -> Vec<Diagnostic> {
	// Server REL uses its dedicated front-end parser.
	if kind == RelSourceKind::Server {
		return match parse_server_source(source) {
			Ok(_) => Vec::new(),
			Err(error) => vec![make_diagnostic(
				error.line,
				error.column,
				DiagnosticSeverity::ERROR,
				"REL1101",
				error.message,
			)],
		};
	}

	// Step 1: Lex the buffer into tokens.
	let tokens = match Lexer::new(source).tokenize() {
		Ok(tokens) => tokens,
		Err(lex_err) => {
			return vec![make_diagnostic(
				lex_err.line,
				lex_err.column,
				DiagnosticSeverity::ERROR,
				"REL1001",
				lex_err.message,
			)];
		}
	};

	// Step 2 & 3: Parse according to source role, then run semantic analysis where applicable.
	let mut diagnostics = Vec::new();
	match kind {
		RelSourceKind::Route => {
			let (parsed, parse_errors) = Parser::new(tokens).parse_file_collecting();
			for err in parse_errors {
				diagnostics.push(make_diagnostic(
					err.line,
					err.column,
					DiagnosticSeverity::ERROR,
					"REL1101",
					err.message,
				));
			}
			if let Some(route_file) = parsed {
				for diag in analyze(&route_file) {
					let severity = match diag.severity {
						Severity::Error => DiagnosticSeverity::ERROR,
						Severity::Warning => DiagnosticSeverity::WARNING,
					};
					let (line, col) = diag
						.symbol
						.as_deref()
						.and_then(|sym| find_symbol_position(source, sym))
						.unwrap_or((1, 1));
					diagnostics.push(make_diagnostic(
						line,
						col,
						severity,
						diag.code,
						diag.message,
					));
				}
			}
		}
		RelSourceKind::Module => {
			if let Err(err) = Parser::new(tokens).parse_module_file() {
				diagnostics.push(make_diagnostic(
					err.line,
					err.column,
					DiagnosticSeverity::ERROR,
					"REL1101",
					err.message,
				));
			}
		}
		RelSourceKind::Service => {
			if let Err(err) = Parser::new(tokens).parse_service_file() {
				diagnostics.push(make_diagnostic(
					err.line,
					err.column,
					DiagnosticSeverity::ERROR,
					"REL1101",
					err.message,
				));
			}
		}
		RelSourceKind::Field => {
			if let Err(err) = Parser::new(tokens).parse_field_file() {
				diagnostics.push(make_diagnostic(
					err.line,
					err.column,
					DiagnosticSeverity::ERROR,
					"REL1304",
					err.message,
				));
			}
		}
		RelSourceKind::Server => unreachable!(),
	}

	diagnostics
}

/// Converts 1-based compiler line/column coordinates into a 0-based LSP Diagnostic.
fn make_diagnostic(
	line: usize,
	column: usize,
	severity: DiagnosticSeverity,
	code: &str,
	message: String,
) -> Diagnostic {
	let zero_line = line.saturating_sub(1) as u32;
	let zero_col = column.saturating_sub(1) as u32;
	Diagnostic {
		range: Range {
			start: Position::new(zero_line, zero_col),
			end: Position::new(zero_line, zero_col.saturating_add(1)),
		},
		severity: Some(severity),
		code: Some(NumberOrString::String(code.to_string())),
		source: Some("rel-lsp".to_string()),
		message,
		..Default::default()
	}
}

/// Locates the first 1-based (line, column) occurrence of an identifier in source text.
/// Used to anchor analyzer diagnostics that report a symbol name without a byte span.
fn find_symbol_position(source: &str, symbol: &str) -> Option<(usize, usize)> {
	if symbol.is_empty() {
		return None;
	}
	for (idx, line) in source.lines().enumerate() {
		if let Some(col) = line.find(symbol) {
			return Some((idx + 1, col + 1));
		}
	}
	None
}

#[cfg(test)]
mod tests {
	use super::*;

	#[test]
	fn reports_lexical_error_with_rel1001() {
		let diags = diagnose_source("const x = \"unterminated", RelSourceKind::Route);
		assert!(!diags.is_empty());
		assert_eq!(
			diags[0].code,
			Some(NumberOrString::String("REL1001".to_string()))
		);
	}

	#[test]
	fn reports_undefined_symbol_in_route() {
		let src = "GET {\n\treturn missingVar;\n}\n";
		let diags = diagnose_source(src, RelSourceKind::Route);
		assert!(diags.iter().any(|d| {
			d.code == Some(NumberOrString::String("E2001".to_string()))
				|| d.severity == Some(DiagnosticSeverity::ERROR)
		}));
	}
}
