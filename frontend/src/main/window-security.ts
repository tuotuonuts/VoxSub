/** Local documents only: one shared boundary for windows, permissions and privileged IPC. */
import type { BrowserWindow, IpcMain, IpcMainInvokeEvent, Session, WebContents } from "electron";
import { pathToFileURL, URL } from "node:url";

interface LocalDocument { owner: BrowserWindow; url: string; privileged: boolean }
const documents = new WeakMap<WebContents, LocalDocument>();
const lockedSessions = new WeakSet<Session>();

function documentURL(value: string): string {
  try { const url = new URL(value); url.hash = ""; return url.href; }
  catch { return ""; }
}

function trustedContents(contents: WebContents | null): boolean {
  if (!contents || contents.isDestroyed()) return false;
  const record = documents.get(contents);
  if (!record || record.owner.isDestroyed()) return false;
  return documentURL(contents.getURL()) === record.url && documentURL(contents.mainFrame.url) === record.url;
}

/** Scoped selection callbacks also validate the top frame, not just the WebContents. */
export function isTrustedDocument(event: Pick<IpcMainInvokeEvent, "sender" | "senderFrame">, expected?: WebContents): boolean {
  return (!expected || event.sender === expected) && trustedContents(event.sender)
    && event.senderFrame === event.sender.mainFrame;
}

function restrictPermissions(session: Session): void {
  if (lockedSessions.has(session)) return;
  lockedSessions.add(session);
  // Audio/capture are implemented by the sidecar/main process. Renderers need no device permissions.
  // Retain only writing user-requested plain text; never grant clipboard read, camera or microphone.
  const allowed = (contents: WebContents | null, permission: string): boolean =>
    permission === "clipboard-sanitized-write" && trustedContents(contents);
  session.setPermissionCheckHandler((contents, permission) => allowed(contents, permission));
  session.setPermissionRequestHandler((contents, permission, callback) => callback(allowed(contents, permission)));
}

export function protectWindow(win: BrowserWindow, file: string, privileged = true): void {
  const contents = win.webContents;
  documents.set(contents, {owner:win, url:pathToFileURL(file).href, privileged});
  const prevent = (event: { preventDefault(): void }): void => event.preventDefault();
  contents.on("will-navigate", prevent);
  contents.on("will-frame-navigate", prevent);
  contents.on("will-redirect", prevent);
  contents.on("will-attach-webview", prevent);
  contents.setWindowOpenHandler(() => ({action:"deny"}));
  contents.once("destroyed", () => documents.delete(contents));
  restrictPermissions(contents.session);
}

export function guardedIpc(ipc: Pick<IpcMain, "handle">): Pick<IpcMain, "handle"> {
  return {handle(channel, listener) {
    ipc.handle(channel, (event, ...args: unknown[]) => {
      if (!isTrustedDocument(event) || !documents.get(event.sender)?.privileged) {
        throw new Error("Untrusted IPC sender");
      }
      return listener(event, ...args);
    });
  }};
}
