import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const launcher = readFileSync(join(here, "..", "scripts", "launch.mjs"), "utf8");

const switchPush = launcher.indexOf('cliArgs.push("--remote-debugging-port=9222")');
const rootPush = launcher.indexOf("cliArgs.push(ROOT)");
assert.notEqual(switchPush, -1, "debug switch is constructed");
assert.notEqual(rootPush, -1, "app path is constructed");
assert.ok(switchPush < rootPush, "Electron debug switch must precede app path");
assert.match(launcher, /stdio:\s*\[\"ignore\",\s*\"inherit\",\s*\"inherit\"\]/);
console.log("launcher debug argument test: 2 passed / 0 failed");
