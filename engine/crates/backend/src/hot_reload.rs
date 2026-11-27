//! Sub-second dev-mode hot reload for compiled REL route tables.
//!
//! Keeps in-flight HTTP requests and streaming/WebSocket connections on the
//! previous snapshot via `ArcSwap`, watches `.route`, `.module`, `.service`,
//! `.field`, and `server.server` files with a 75 ms debounce window, computes
//! affected reverse dependents via `route_engine::SymbolDependencyGraph`, and
//! preserves the last known-good table whenever a save contains syntax or type
//! errors.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use arc_swap::ArcSwap;
use notify::{Event, RecommendedWatcher, RecursiveMode, Watcher};
use route_engine::{SourceId, SymbolDependencyGraph};

/// Default debounce duration (75 ms) to coalesce rapid editor save bursts.
pub const DEFAULT_DEBOUNCE_MS: u64 = 75;

/// Holds the currently active compiled route table for lock-free request reads.
pub struct LiveRouteTable<T> {
	active: ArcSwap<T>,
}

impl<T> LiveRouteTable<T> {
	/// Initializes the live table with the startup compilation snapshot.
	pub fn new(initial: T) -> Self {
		Self {
			active: ArcSwap::from_pointee(initial),
		}
	}

	/// Returns a lock-free handle to the current route table for an incoming request.
	pub fn load(&self) -> Arc<T> {
		self.active.load_full()
	}

	/// Atomically swaps in a newly compiled route table without dropping active connections.
	pub fn swap(&self, next: T) {
		self.active.store(Arc::new(next));
	}
}

/// Shared hot-reload diagnostic state readable by the terminal logger and the
/// embedded admin dashboard (`api/src/dashboard.rs`).
#[derive(Debug, Default, Clone)]
pub struct DevReloadStatus {
	diagnostics: Arc<RwLock<Vec<String>>>,
}

impl DevReloadStatus {
	/// Creates a clean initial reload status with zero diagnostics.
	pub fn new() -> Self {
		Self {
			diagnostics: Arc::new(RwLock::new(Vec::new())),
		}
	}

	/// Clears diagnostics after a successful incremental hot reload.
	pub fn record_success(&self, changed_count: usize, elapsed: Duration) {
		if let Ok(mut guard) = self.diagnostics.write() {
			guard.clear();
		}
		tracing::info!(
			target: "rbe::hot_reload",
			changed_files = changed_count,
			elapsed_ms = elapsed.as_millis(),
			"REL hot reload succeeded; swapped active route table"
		);
	}

	/// Records compile diagnostics while keeping the last known-good route table active.
	pub fn record_failure(&self, errors: &[String]) {
		if let Ok(mut guard) = self.diagnostics.write() {
			*guard = errors.to_vec();
		}
		for err in errors {
			tracing::error!(
				target: "rbe::hot_reload",
				"REL hot reload diagnostic (keeping last known-good route table): {err}"
			);
		}
	}

	/// Returns the most recent compile diagnostics for dashboard rendering.
	pub fn current_diagnostics(&self) -> Vec<String> {
		self.diagnostics
			.read()
			.map(|guard| guard.clone())
			.unwrap_or_default()
	}
}

/// Returns true when `args` enables dev-mode hot reload (`backend dev` or `--dev`).
pub fn dev_mode_requested(args: &[String]) -> bool {
	args.iter().any(|arg| arg == "dev" || arg == "--dev")
}

/// Returns true if `path` is a REL source file monitored by dev-mode hot reload.
pub fn is_watched_rel_path(path: &Path) -> bool {
	let Some(file_name) = path.file_name().and_then(|n| n.to_str()) else {
		return false;
	};
	if file_name == "server.server" {
		return true;
	}
	matches!(
		path.extension().and_then(|ext| ext.to_str()),
		Some("route" | "module" | "service" | "field")
	)
}

/// Recompiles only affected files when the watcher reports a change.
/// On compile error, keeps the last known-good table active and returns diagnostics.
pub fn handle_source_change<T, F>(
	table: &LiveRouteTable<T>,
	changed_paths: &[PathBuf],
	recompile: F,
) -> Result<(), Vec<String>>
where
	F: FnOnce(&[PathBuf]) -> Result<T, Vec<String>>,
{
	// Only swap the active router when incremental compilation succeeds cleanly.
	let next_table = recompile(changed_paths)?;
	table.swap(next_table);
	Ok(())
}

/// Computes the minimal set of `SourceId`s that must be recompiled when
/// `changed_sources` are modified, by traversing reverse edges in `graph`.
pub fn affected_sources_for_change(
	graph: &SymbolDependencyGraph,
	changed_sources: &[SourceId],
) -> BTreeSet<SourceId> {
	// Build reverse source-level adjacency map: target_source -> {dependent_sources}.
	let mut reverse_edges: HashMap<SourceId, BTreeSet<SourceId>> = HashMap::new();
	for symbol in graph.symbols() {
		for dep in graph.dependencies(symbol) {
			reverse_edges
				.entry(dep.source.clone())
				.or_default()
				.insert(symbol.source.clone());
		}
	}

	// BFS from every modified source to collect all transitive reverse dependents.
	let mut affected = BTreeSet::new();
	let mut queue = VecDeque::new();
	for src in changed_sources {
		if affected.insert(src.clone()) {
			queue.push_back(src.clone());
		}
	}
	while let Some(current) = queue.pop_front() {
		if let Some(dependents) = reverse_edges.get(&current) {
			for dependent in dependents {
				if affected.insert(dependent.clone()) {
					queue.push_back(dependent.clone());
				}
			}
		}
	}
	affected
}

/// Spawns a background `notify` watcher rooted at `project_root` with a 75 ms
/// debounce window. Returns the watcher handle (keep alive for the lifetime of dev mode).
pub fn spawn_debounced_watcher<T, F>(
	project_root: &Path,
	table: Arc<LiveRouteTable<T>>,
	status: DevReloadStatus,
	mut recompile: F,
) -> anyhow::Result<RecommendedWatcher>
where
	T: Send + Sync + 'static,
	F: FnMut(&[PathBuf]) -> Result<T, Vec<String>> + Send + 'static,
{
	let (tx, rx) = std::sync::mpsc::channel::<PathBuf>();
	let mut watcher = notify::recommended_watcher(move |res: notify::Result<Event>| {
		if let Ok(event) = res {
			for path in event.paths {
				if is_watched_rel_path(&path) {
					let _ = tx.send(path);
				}
			}
		}
	})?;
	watcher.watch(project_root, RecursiveMode::Recursive)?;

	let debounce = Duration::from_millis(DEFAULT_DEBOUNCE_MS);
	std::thread::Builder::new()
		.name("rbe-rel-hot-reload".to_string())
		.spawn(move || {
			while let Ok(first_path) = rx.recv() {
				let mut batch = BTreeSet::new();
				batch.insert(first_path);
				// Drain any additional filesystem events arriving within the 75 ms window.
				let deadline = Instant::now() + debounce;
				while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
					match rx.recv_timeout(remaining) {
						Ok(next_path) => {
							batch.insert(next_path);
						}
						Err(_) => break,
					}
				}
				let changed: Vec<PathBuf> = batch.into_iter().collect();
				let started = Instant::now();
				match handle_source_change(&table, &changed, |paths| recompile(paths)) {
					Ok(()) => status.record_success(changed.len(), started.elapsed()),
					Err(errors) => status.record_failure(&errors),
				}
			}
		})?;

	Ok(watcher)
}

#[cfg(test)]
mod tests {
	use super::*;
	use route_engine::{RelSourceKind, SymbolId};

	#[test]
	fn swaps_table_on_clean_compile_and_preserves_last_good_on_error() {
		let table = LiveRouteTable::new("v1".to_string());
		let in_flight = table.load();
		assert_eq!(*in_flight, "v1");

		// Clean recompile swaps active table while in-flight handle retains v1.
		handle_source_change(&table, &[PathBuf::from("api/hello.route")], |_| {
			Ok("v2".to_string())
		})
		.unwrap();
		assert_eq!(*in_flight, "v1");
		assert_eq!(*table.load(), "v2");

		// Failed recompile keeps v2 active and returns diagnostics.
		let err = handle_source_change(&table, &[PathBuf::from("api/hello.route")], |_| {
			Err(vec!["REL1101: unexpected token".to_string()])
		})
		.unwrap_err();
		assert_eq!(err.len(), 1);
		assert_eq!(*table.load(), "v2");
	}

	#[test]
	fn watches_only_rel_source_files() {
		assert!(is_watched_rel_path(Path::new("api/users.route")));
		assert!(is_watched_rel_path(Path::new("module/auth.module")));
		assert!(is_watched_rel_path(Path::new("service/mail.service")));
		assert!(is_watched_rel_path(Path::new("api/user.field")));
		assert!(is_watched_rel_path(Path::new("server.server")));
		assert!(!is_watched_rel_path(Path::new("README.md")));
		assert!(!is_watched_rel_path(Path::new("target/debug/backend")));
	}

	#[test]
	fn resolves_reverse_dependents_from_dependency_graph() {
		let mut graph = SymbolDependencyGraph::default();
		let mod_a = SourceId::physical(RelSourceKind::Module, "a").unwrap();
		let route_b = SourceId::physical(RelSourceKind::Route, "b").unwrap();
		let route_c = SourceId::physical(RelSourceKind::Route, "c").unwrap();
		graph.add_edge(
			SymbolId::new(route_b.clone(), "GET"),
			SymbolId::new(mod_a.clone(), "helper"),
		);
		graph.add_symbol(SymbolId::new(route_c.clone(), "GET"));

		let affected = affected_sources_for_change(&graph, &[mod_a.clone()]);
		assert!(affected.contains(&mod_a));
		assert!(affected.contains(&route_b));
		assert!(!affected.contains(&route_c));
	}
}
