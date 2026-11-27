# RBE REL Extension for VS Code

Minimal VS Code extension providing syntax highlighting, live diagnostics, SDK-aware autocomplete, hover documentation, and go-to-definition for RBE REL files (`.route`, `.module`, `.service`, `.field`, and `server.server`).

## How It Works

The extension spawns `rpx lsp` over stdio. Because `rpx lsp` resolves the active project's `.rbe/bin/rel-lsp` binary, the extension automatically stays in sync with your project's activated RBE SDK toolchain.

## Local Development

```bash
cd editors/vscode-rel
npm install
```

Open the folder in VS Code and press `F5` to launch an Extension Development Host.
