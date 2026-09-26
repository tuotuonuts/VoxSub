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

import { guessStderrLevel, localIsoNow, splitStderrLines } from "../shared/log-levels";
import { REQUEST_HARD_DEADLINE_MS, SLOW_REQUEST_NOTICE_MS } from "../shared/request-outcome";

export type BackendEvent =
  | { type: "ready"; version: string }
  | { type: "status"; text: string }
  | { type: "disconnected"; reason?: string }
  | { type: "request-timeout"; command: string; hint?: string }
  | { type: "utterance"; source: string; translation: string }
  | { type: "draft"; source: string; translation: string }
  | { type: "partial"; text: string }
  | { type: "progress"; completed: number; total: number; stage: string }
  | { type: "error"; message: string }
  | { type: "log"; ts: string; level: string; message: string };

export interface CommandResult {
  ok: boolean;
  error?: string;
  code?: string;
  jobId?: string;
  data?: unknown;
  /** 请求未在时限内返回：**不是失败**，后端可能仍在处理（见下方 command()）。 */
  timedOut?: boolean;
  /** 请求没能发出（后端未运行 / 未初始化）。 */
  unavailable?: boolean;
  /** 命令是否确定未发送、收到回复或仍处于不确定状态。 */
  delivery?: "not_sent" | "unknown" | "response";
}

type Listener = (event: BackendEvent) => void;

/** 一次待返回的请求。 */
interface PendingRequest {
  resolve: (result: CommandResult) => void;
  /** 已经发过"超过时限"的通知，避免重复刷。 */
  notified: boolean;
  /** 首次通知的定时器。 */
  noticeTimer: NodeJS.Timeout;
  /** 硬性上限的定时器。 */
  deadlineTimer: NodeJS.Timeout;
}

export class BackendBridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private listeners: Listener[] = [];
  private pending = new Map<number, PendingRequest>();
  private nextId = 1;
  private disposed = false;

  onEvent(listener: Listener): void {
    this.listeners.push(listener);
  }

  /** 后端是否已在运行（渲染层重载后重新连上时要靠它决定补发 ready）。 */
  isRunning(): boolean {
    return this.child !== null;
  }

  private emit(event: BackendEvent): void {
    for (const listener of this.listeners) listener(event);
  }

  private resolvePython(): { command: string; args: string[] } {
    // 两种运行形态，解析方式完全不同：
    //
    // **源码运行**（开发期）
    //   <repo>/frontend/          ← 本项目的 package.json 与 src/
    //   <repo>/voxsub/            ← Python 包
    //   <repo>/.venv/             ← 解释器与依赖
    //   用仓库自带的 venv 跑 backend/ipc_server.py。
    //   不写死相对层级：逐级向上找，这样仓库调整结构时不会静默失效。
    //
    // **打包运行**（安装后）
    //   <安装目录>/resources/backend/VoxSubBackend.exe
    //   sidecar 是 PyInstaller 产物，自带解释器，不需要 venv，
    //   也不能去找"仓库根" —— 装完的机器上没有仓库。
    const explicit = process.env["VOXSUB_ROOT"];
    const appPath = app.getAppPath();

    // ---- 打包形态：优先用 sidecar ----
    const resourcesPath = process.resourcesPath ?? "";
    const packaged = path.join(resourcesPath, "backend", "VoxSubBackend.exe");
    if (fs.existsSync(packaged)) {
      return { command: packaged, args: [] };
    }

    // ---- 源码形态 ----
    const looksLikeRepoRoot = (dir: string): boolean =>
      fs.existsSync(path.join(dir, "voxsub", "__init__.py")) ||
      fs.existsSync(path.join(dir, ".venv", "Scripts", "python.exe"));

    let voxsubRoot = "";
    if (explicit && looksLikeRepoRoot(explicit)) {
      voxsubRoot = explicit;
    } else {
      let probe = appPath;
      for (let depth = 0; depth < 4; depth += 1) {
        if (looksLikeRepoRoot(probe)) {
          voxsubRoot = probe;
          break;
        }
        const parent = path.dirname(probe);
        if (parent === probe) break;
        probe = parent;
      }
    }
    if (!voxsubRoot) {
      voxsubRoot = path.resolve(appPath, "..");
    }

    const python = path.join(voxsubRoot, ".venv", "Scripts", "python.exe");

    // 开发期：源码目录；打包后：extraResources 里的 backend/
    const candidates = [
      path.join(appPath, "backend", "ipc_server.py"),
      path.join(resourcesPath, "backend", "ipc_server.py"),
    ];
    const entry = candidates.find((candidate) => fs.existsSync(candidate));

    if (!entry) {
      throw new Error(
        `找不到后端入口。已查找 sidecar：${packaged}；脚本：${candidates.join(" | ")}`,
      );
    }
    if (!fs.existsSync(python)) {
      throw new Error(
        `找不到 Python 解释器：${python}（仓库根解析为 ${voxsubRoot}；可用 VOXSUB_ROOT 指定）`,
      );
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
      // 后端的 stderr 转发到界面日志区。
      //
      // 注意：**不能一律标成 error**。原先就是那么写的，而 Python 侧会把
      // 整个日志流也写到 stderr，于是界面上每条 INFO 都显示成 ERROR
      // （用户报的就是这个）。这里按行拆分并识别真实级别。
      for (const line of splitStderrLines(chunk)) {
        this.emit({
          type: "log",
          ts: localIsoNow(),
          level: guessStderrLevel(line),
          message: line,
        });
      }
    });

    child.on("exit", (code) => {
      this.child = null;
      // 独立事件：后端**退出了**，不是"启动失败"，也不是一句提示文案。
      // 渲染层据此进入 disconnected 状态，把"运行中"收回去（缺陷 #11）。
      this.emit({ type: "disconnected", reason: `code=${code ?? "null"}` });
      this.emit({ type: "log", ts: localIsoNow(), level: "WARNING", message: `后端已退出（code=${code ?? "null"}）` });
      for (const [id, request] of this.pending) {
        this.settle(id, request, {
          ok: false,
          unavailable: true,
          error: "后端已退出",
          delivery: "unknown",
        });
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
      this.emit({ type: "log", ts: localIsoNow(), level: "INFO", message: trimmed });
      return;
    }

    const id = payload["id"];
    if (typeof id === "number") {
      const request = this.pending.get(id);
      if (request) {
        const ok = payload["ok"] !== false;
        this.settle(id, request, {
          ok,
          delivery: "response",
          ...(typeof payload["code"] === "string" ? { code: payload["code"] } : {}),
          ...(typeof payload["jobId"] === "string" ? { jobId: payload["jobId"] } : {}),
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
    if (!this.child) {
      // 请求根本没发出去 —— 与"后端报错"分开，界面能据此说清是哪种情况。
      return Promise.resolve({ ok: false, unavailable: true, error: "后端未运行", delivery: "not_sent" });
    }
    const id = this.nextId++;
    const payload = JSON.stringify({ id, command: name, args }) + "\n";
    return new Promise<CommandResult>((resolve) => {
      // ---- 超时处理（缺陷 #4）----
      //
      // 原实现在 30 秒后把请求**判成失败**（`ok:false, error:"命令超时"`）并从
      // pending 里删掉。两处后果都很严重：
      //   · 界面记 ERROR「失败: 命令超时」—— 而迁移/下载/OCR 这些长任务这时候
      //     还在正常跑（30 秒对它们只是起步）；
      //   · 后端**稍后送回的真实结果会被丢弃**（handleLine 找不到请求就 return），
      //     调用点于是拿不到终态，还会顺手解除退出保护（migration.ts 的
      //     setBusy(false)）—— 用户此时退出就留下半个模型库。
      //
      // 现在的语义：
      //   · 到时限只发一条**通知**（request-timeout），请求保持挂起，任务状态不变；
      //   · 后端真的返回时，这个 promise 用**真实结果**兑现（数据不丢）；
      //   · 只有超过硬性上限（30 分钟）才兑现成 timedOut —— 仍然**不是 failed**，
      //     且明确告诉调用点"我们没拿到结果"，而不是"任务失败了"。
      const noticeTimer = setTimeout(() => {
        const request = this.pending.get(id);
        if (!request || request.notified) return;
        request.notified = true;
        this.emit({
          type: "request-timeout",
          command: name,
          hint: `${name} 超过 ${Math.round(SLOW_REQUEST_NOTICE_MS / 1000)} 秒未返回：任务仍在进行`,
        });
      }, SLOW_REQUEST_NOTICE_MS);

      const deadlineTimer = setTimeout(() => {
        const request = this.pending.get(id);
        if (!request) return;
        this.settle(id, request, {
          ok: false,
          timedOut: true,
          error: `请求未在 ${Math.round(REQUEST_HARD_DEADLINE_MS / 60_000)} 分钟内返回（不代表任务失败，也不代表已取消）`,
          delivery: "unknown",
        });
      }, REQUEST_HARD_DEADLINE_MS);

      this.pending.set(id, { resolve, notified: false, noticeTimer, deadlineTimer });
      this.child?.stdin.write(payload, (error) => {
        if (error) {
          const request = this.pending.get(id);
          if (request) {
            this.settle(id, request, { ok: false, error: error.message, delivery: "unknown" });
          }
        }
      });
    });
  }

  /**
   * 兑现一次请求：清掉两个定时器、从 pending 移除、再 resolve。
   *
   * 收在一处是为了保证**每个出口都清定时器** —— 漏掉一个就会留下一个
   * 30 分钟的悬挂定时器（Electron 退出时表现为进程迟迟不退）。
   */
  private settle(id: number, request: PendingRequest, result: CommandResult): void {
    clearTimeout(request.noticeTimer);
    clearTimeout(request.deadlineTimer);
    this.pending.delete(id);
    request.resolve(result);
  }

  stop(): CommandResult {
    if (!this.child) return { ok: true };
    void this.command("shutdown", null).catch(() => undefined);
    return { ok: true };
  }

  dispose(): void {
    this.disposed = true;
    const child = this.child;
    this.child = null;
    for (const [id, request] of this.pending) {
      this.settle(id, request, { ok: false, unavailable: true, error: "正在退出", delivery: "unknown" });
    }
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
