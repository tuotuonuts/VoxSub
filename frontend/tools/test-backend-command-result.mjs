import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { createReporter, ROOT } from "./esbuild-ts.mjs";

const { check, finish } = createReporter("主进程 BackendBridge command result contract");
const scratch = mkdtempSync(join(tmpdir(), "voxsub-backend-command-result-"));
process.on("exit", () => {
  try { rmSync(scratch, { recursive: true, force: true }); } catch {}
});

try {
  const esbuild = join(ROOT, "node_modules", "esbuild", "bin", "esbuild");
  const source = join(ROOT, "src", "main", "backend.ts");
  const electronStub = join(scratch, "electron-stub.mjs");
  const output = join(scratch, "backend-bridge.mjs");
  writeFileSync(electronStub, "export const app = {};\n", "utf8");
  if (!existsSync(esbuild)) throw new Error(`esbuild not found: ${esbuild}`);
  const compiled = spawnSync(process.execPath, [esbuild,
    source,
    "--bundle",
    "--format=esm",
    "--platform=node",
    "--target=node20",
    `--outfile=${output}`,
    `--alias:electron=${electronStub}`,
  ], {
    cwd: ROOT,
    stdio: "pipe",
    encoding: "utf8",
  });
  if (compiled.status !== 0) {
    throw new Error(`esbuild failed: ${compiled.stderr || compiled.stdout || compiled.error || compiled.status}`);
  }
  const { BackendBridge } = await import(pathToFileURL(output).href);
  const bridge = new BackendBridge();
  const child = {
    stdin: {
      write(serialized, callback) {
        const request = JSON.parse(serialized);
        queueMicrotask(() => bridge.handleLine(JSON.stringify({
          id: request.id,
          ok: false,
          code: "active_job_exists",
          jobId: "migration-owner-42",
          error: "已有迁移任务仍在运行",
        })));
        callback?.(null);
        return true;
      },
      end() {},
    },
    stdout: { setEncoding() {}, on() {} },
    stderr: { setEncoding() {}, on() {} },
    on() { return this; },
    kill() { return true; },
  };
  bridge.child = child;

  const conflict = await bridge.command("start_migration", {
    async: true,
    clientMigrationId: "migration-renderer-reloaded",
  });
  check("收到 IPC error response 后保留稳定拒绝码", conflict.code === "active_job_exists", JSON.stringify(conflict));
  check("收到 IPC error response 后保留原活动 jobId", conflict.jobId === "migration-owner-42", JSON.stringify(conflict));
  check("明确收到后端响应时标记 delivery=response", conflict.delivery === "response", JSON.stringify(conflict));
  bridge.dispose();

  const noBackend = await new BackendBridge().command("start_migration", { async: true });
  check("后端未启动时准确标记请求 not_sent", noBackend.delivery === "not_sent", JSON.stringify(noBackend));
} catch (error) {
  check("BackendBridge command result behavior harness loads and runs", false, error instanceof Error ? error.stack : String(error));
}

finish();
