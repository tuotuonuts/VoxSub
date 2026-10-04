/** Electron-owned registration and small atomic preference file, separate from conversation/config data. */
import * as fs from "node:fs";
import * as path from "node:path";
import { emptyBindings, type ShortcutBindings } from "../shared/shortcuts";
import { ShortcutManager, type ShortcutDriver } from "./shortcut-manager";
import { ShortcutActions, type ShortcutActionDependencies } from "./shortcut-actions";
export function createShortcutService(directory: string, deps: ShortcutActionDependencies, changed: () => void, driver: ShortcutDriver): ShortcutManager {
  const file = path.join(directory, "global-shortcuts.json");
  const persist = (bindings: ShortcutBindings): void => {
    fs.mkdirSync(directory, { recursive: true });
    const temp = file + "." + process.pid + "-" + Date.now() + ".tmp";
    try { fs.writeFileSync(temp, JSON.stringify({ version: 1, bindings }, null, 2), { encoding: "utf8", flag: "wx" }); fs.renameSync(temp, file); }
    finally { if (fs.existsSync(temp)) fs.unlinkSync(temp); }
  };
  const runner = new ShortcutActions(deps);
  const manager = new ShortcutManager(driver, persist, action => { void runner.run(action); }, changed);
  try {
    if (!fs.existsSync(file)) manager.load(emptyBindings());
    else {
      if (fs.statSync(file).size > 16 * 1024) throw new Error("Shortcut settings exceed size limit");
      const parsed = JSON.parse(fs.readFileSync(file, "utf8")) as { version?: unknown; bindings?: unknown };
      manager.load(parsed?.bindings, parsed?.version !== 1);
    }
  } catch { manager.load(emptyBindings(), true); }
  return manager;
}
