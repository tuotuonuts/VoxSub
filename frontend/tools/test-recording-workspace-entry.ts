/** Single bundle: the workspace and backend connection must share the production store. */
export { buildWorkspace } from "../src/renderer/views/workspace";
export { store, connectBackend, refreshSessionState } from "../src/renderer/store";
export { CMD } from "../src/renderer/protocol";
export { setLanguage, tr } from "../src/renderer/i18n";
