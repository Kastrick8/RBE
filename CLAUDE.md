# CLAUDE.md

Read and follow `AGENTS.md` in the repository root before making any changes.

## Quick Non-Negotiables

- **Docs live in `doc/` ONLY** — never write to `docs/`. Every new feature/API/CLI change must be documented **in source comments AND in `doc/`**.
- **Never delete legacy docs blindly** — diff and merge any accurate details into `doc/` first.
- **Tab indentation (`\t`)** and clear onboarding comments on all new/edited code.
- **Targeted Cargo runs** (`cargo check -p <crate>`, `cargo test -p <crate>`) to keep local builds fast.
- **No junk commits** — no empty stubs, scratch resume notes, or runtime `data/` state files.
