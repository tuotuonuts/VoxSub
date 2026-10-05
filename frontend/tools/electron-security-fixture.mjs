/** Electron API-shaped fixture. Production security checks stay enabled in VM tests. */
import {pathToFileURL} from 'node:url';
export function securityContents(overrides = {}) {
  const mainFrame={url:'about:blank'};
  return {mainFrame, getURL:()=>mainFrame.url, isDestroyed:()=>false, on(){}, once(){}, setWindowOpenHandler(){},
    session:{setPermissionCheckHandler(){},setPermissionRequestHandler(){}}, ...overrides};
}
export function loadFixturePage(win, file) {win.webContents.mainFrame.url=pathToFileURL(file).href;}
export function senderEvent(contents) {return {sender:contents,senderFrame:contents.mainFrame};}
