// Minimal VS Code LSP client for RBE's Runtime Engine Language (REL).
// Spawns `rpx lsp` over stdio so every workspace automatically uses the exact
// `rel-lsp` binary matching the project's activated SDK toolchain.

const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client;

/**
 * Activates the REL extension and starts the `rpx lsp` language server client.
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
	const config = vscode.workspace.getConfiguration("rel.lsp");
	const command = config.get("command", "rpx");
	const args = config.get("args", ["lsp"]);

	const serverOptions = {
		command,
		args,
		transport: TransportKind.stdio,
	};

	const clientOptions = {
		documentSelector: [
			{ scheme: "file", language: "rel" },
			{ scheme: "untitled", language: "rel" },
		],
		synchronize: {
			fileEvents: vscode.workspace.createFileSystemWatcher(
				"**/*.{route,module,service,field,server}"
			),
		},
	};

	client = new LanguageClient(
		"rel-lsp",
		"RBE REL Language Server",
		serverOptions,
		clientOptions
	);

	context.subscriptions.push(client);
	client.start();
}

/**
 * Cleanly stops the running `rpx lsp` child process when VS Code shuts down.
 */
function deactivate() {
	if (!client) {
		return undefined;
	}
	return client.stop();
}

module.exports = {
	activate,
	deactivate,
};
