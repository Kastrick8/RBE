# Sub-Second Hot Reload (`rbe dev` / `rpx dev`)

RBE's dev-mode hot reload (`engine/crates/backend/src/hot_reload.rs`) lets you edit `.route`, `.module`, `.service`, `.field`, and `server.server` files and observe changes in under a second without restarting the backend server or dropping active connections.

## Architecture

1. **Debounced File Watcher (`notify`):** Watches the project tree for changes to `.route`, `.module`, `.service`, `.field`, and `server.server` with a **75 ms debounce** window (`DEFAULT_DEBOUNCE_MS = 75`) to coalesce rapid multi-file editor saves.
2. **Incremental Reverse-Dependent Graph (`SymbolDependencyGraph`):** Uses `route-engine/src/dependency_graph.rs` (`affected_sources_for_change`) to compute only the modified files and their transitive reverse dependents so RELC recompiles the minimal affected subgraph.
3. **Lock-Free Atomic Route-Table Swap (`ArcSwap`):** `LiveRouteTable<T>` wraps the active compiled router in `ArcSwap<T>`. In-flight HTTP requests and open streams/WebSockets keep reading their existing `Arc<T>` snapshot until they finish, while new requests immediately read the newly swapped table.
4. **Error Preservation:** If a save contains a syntax or type error, `handle_source_change` leaves the last known-good route table active and publishes the diagnostics through `DevReloadStatus` to both the terminal logger (`logging/src/terminal.rs`) and the admin dashboard (`api/src/dashboard.rs`).

## Usage

Start dev mode from your activated project directory:

```text
rpx dev
```

Or invoke the backend binary directly:

```text
backend dev
```
