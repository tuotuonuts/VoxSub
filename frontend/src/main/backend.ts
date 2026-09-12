/**
 * Python 后端桥。
 *
 * 契约：voxsub 包一行不改，作为独立进程被拉起；
 * 主进程只做三件事 —— 发命令、收事件、在退出时清理。
 *
 * 传输方式：stdin/stdout 行分隔 JSON（NDJSON）。
 * 选择理由：不需要开端口（避免防火墙弹窗与端口占用），
 * 且事件是低频的（句子级，不是逐帧），吞吐压力很小。
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as readline from "node:readline";
import { app } from "electron";

export type BackendEvent =
  | { type: "ready"; version: string }
  | { type: "status"; text: string }
  | { type: "utterance"; source: string; translation: string }
  | { type: "draft"; source: string; translation: string }
  | { type: "partial"; text: string }
  | { type: "progress"; completed: number; total: number; stage: string }
  | { type: "error"; message: string }
  | { type: "log"; level: string; message: string };

export interface CommandResult {
  ok: boolean;
  error?: string;
  data?: unknown;
}

type Listener = (event: BackendEvent) => void;

export class BackendBridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private listeners: Listener[] = [];
  private pending = new Map<number, (result: CommandResult) => void>();
  private nextId = 1;
  private disposed = false;

  onEvent(listener: Listener): void {
    this.listeners.push(listener);
  }

  private emit(event: BackendEvent): void {
    for (const listener of this.listeners) listener(event);
  }

  private resolvePython(): { command: string; args: string[] } {
    // Python 解释器复用 VoxSub 现成的 venv（voxsub 包的依赖都在里面）；
    // 但 IPC 服务是本项目自己的文件，位于 backend/ipc_server.py。
    // 两者分开的原因：VoxSub 仓库必须保持零改动。
    const voxsubRoot =
      process.env["VOXSUB_ROOT"] ?? path.resolve(app.getAppPath(), "..", "VoxSub");
    const python = path.join(voxsubRoot, ".venv", "Scripts", "python.exe");

    // 开发期：源码目录；打包后：extraResources 里的 backend/
    const candidates = [
      path.join(app.getAppPath(), "backend", "ipc_server.py"),
      path.join(process.resourcesPath ?? "", "backend", "ipc_server.py"),
    ];
    const entry = candidates.find((candidate) => fs.existsSync(candidate));

    if (!entry) {
      throw new Error(
        `找不到 backend/ipc_server.py（已查找：${candidates.join(" | ")}）`,
      );
    }
    if (!fs.existsSync(python)) {
      throw new Error(`找不到 Python 解释器：${python}（可用 VOXSUB_ROOT 指定 VoxSub 目录）`);
    }
    return { command: python, args: [entry] };
  }

  start(): CommandResult {
    if (this.disposed) return { ok: false, error: "桥已释放" };
    if (this.child) return { ok: true, data: "已在运行" };

    let resolved: { command: string; args: string[] };
    try {
      resolved = this.resolvePython();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.emit({ type: "error", message });
      return { ok: false, error: message };
    }
    const { command, args } = resolved;
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(command, args, {
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: "1",
          // 必须清掉，否则 Python 侧会 import 到错误的包（见 AGENTS.md 环境事实）
          PYTHONPATH: "",
          PYTHONHOME: "",
        },
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.emit({ type: "error", message: `后端启动失败：${message}` });
      return { ok: false, error: message };
    }

    this.child = child;
    child.stdout.setEncoding("utf-8");
    child.stderr.setEncoding("utf-8");

    const reader = readline.createInterface({ input: child.stdout });
    reader.on("line", (line) => this.handleLine(line));
    child.stderr.on("data", (chunk: string) => {
      // 后端诊断输出原样转发到界面日志区，不阻断
      this.emit({ type: "log", level: "error", message: chunk.trimEnd() });
    });

    child.on("exit", (code) => {
      this.child = null;
      this.emit({ type: "status", text: `后端已退出（code=${code ?? "null"}）` });
      for (const [, resolve] of this.pending) {
        resolve({ ok: false, error: "后端已退出" });
      }
      this.pending.clear();
    });

    return { ok: true };
  }

  private handleLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return;
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      // 非 JSON 行按日志处理，避免后端调试输出造成的噪声中断协议
      this.emit({ type: "log", level: "info", message: trimmed });
      return;
    }

    const id = payload["id"];
    if (typeof id === "number") {
      const resolver = this.pending.get(id);
      if (resolver) {
        this.pending.delete(id);
        const ok = payload["ok"] !== false;
        resolver({
          ok,
          ...(typeof payload["error"] === "string" ? { error: payload["error"] } : {}),
          data: payload["data"],
        });
      }
      return;
    }

    const kind = payload["event"];
    if (typeof kind === "string") {
      this.emit({ ...(payload as object), type: kind } as unknown as BackendEvent);
    }
  }

  command(name: string, args: unknown): Promise<CommandResult> {
    if (!this.child) return Promise.resolve({ ok: false, error: "后端未运行" });
    const id = this.nextId++;
    const payload = JSON.stringify({ id, command: name, args }) + "\n";
    return new Promise<CommandResult>((resolve) => {
      this.pending.set(id, resolve);
      this.child?.stdin.write(payload, (error) => {
        if (error) {
          this.pending.delete(id);
          resolve({ ok: false, error: error.message });
        }
      });
      // 超时兜底：后端卡死时不能永久挂起 UI
      setTimeout(() => {
        if (this.pending.delete(id)) resolve({ ok: false, error: "命令超时" });
      }, 30_000);
    });
  }

  stop(): CommandResult {
    if (!this.child) return { ok: true };
    this.command("shutdown", null).catch(() => undefined);
    return { ok: true };
  }

  dispose(): void {
    this.disposed = true;
    const child = this.child;
    this.child = null;
    for (const [, resolve] of this.pending) resolve({ ok: false, error: "正在退出" });
    this.pending.clear();
    if (!child) return;
    try {
      child.stdin.end();
      child.kill();
    } catch {
      // 退出路径尽力而为；安装器有独立的强制关闭协议
    }
  }
}
