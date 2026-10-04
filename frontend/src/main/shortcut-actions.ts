/** Main-process actions: never rely on a focused/visible renderer or optimistic state. */
import { CMD } from "../renderer/protocol";
import type { CommandResult } from "./backend";
import type { ShortcutAction } from "../shared/shortcuts";
export interface ShortcutActionDependencies {
  command(name: string, args: unknown): Promise<CommandResult>;
  local(action: ShortcutAction): void;
  notice(key: string): void;
  state(data: Record<string, unknown>): void;
  allowed(): boolean;
}
export class ShortcutActions {
  private pending = false;
  constructor(private readonly deps: ShortcutActionDependencies) {}
  async run(action: ShortcutAction): Promise<void> {
    if (["toggle_window", "toggle_overlay", "toggle_click_through"].includes(action)) { this.deps.local(action); return; }
    if (this.pending) { this.deps.notice("会话操作正在进行，请稍候"); return; }
    if (!this.deps.allowed()) { this.deps.notice("当前暂不能执行会话操作，请稍后重试"); return; }
    this.pending = true;
    try { await this.sessionAction(action); }
    catch { this.deps.notice("快捷键操作未确认，请查看诊断日志"); }
    finally { this.pending = false; }
  }
  private async sessionAction(action: ShortcutAction): Promise<void> {
    const jobs = await this.deps.command(CMD.jobList, {});
    const active = (jobs.data as { active?: unknown } | undefined)?.active;
    if (!jobs.ok || !Array.isArray(active)) { this.deps.notice("无法确认会话状态，未执行快捷键操作"); return; }
    if (active.some(name => [CMD.start, CMD.stop, CMD.pause, CMD.resume, CMD.setRecording].includes(name))) { this.deps.notice("会话操作正在进行，请稍候"); return; }
    const result = await this.deps.command(CMD.state, {});
    const data = result.data as Record<string, unknown> | undefined;
    if (!result.ok || !data || typeof data["running"] !== "boolean" || typeof data["paused"] !== "boolean" || !["a", "b", "c", "d"].includes(String(data["mode"]))) { this.deps.notice("无法确认会话状态，未执行快捷键操作"); return; }
    if (data["mode"] === "d") { this.deps.notice("OCR 模式请使用页面上的操作按钮"); return; }
    const running = data["running"];
    if (action === "pause_session" && (!running || !["a", "b"].includes(String(data["mode"])))) { this.deps.notice("当前会话不支持暂停或继续"); return; }
    if (action === "stop_session" && !running) { this.deps.notice("当前没有运行中的会话"); return; }
    if (action === "toggle_recording" && (data["recordingSupported"] !== true || data["recordingCanChange"] !== true || typeof data["recordingEnabled"] !== "boolean")) { this.deps.notice("当前无法切换录音，请先结束会话并确认音源"); return; }
    const commands: Partial<Record<ShortcutAction, string>> = {
      toggle_session: running ? CMD.stop : CMD.start,
      pause_session: data["paused"] ? CMD.resume : CMD.pause,
      toggle_recording: CMD.setRecording, stop_session: CMD.stop,
    };
    const command = commands[action];
    if (!command) return;
    const args = action === "toggle_recording" ? { enabled: !data["recordingEnabled"] } : { async: true };
    const response = await this.deps.command(command, args);
    if (!response.ok) { this.deps.notice(response.timedOut || response.delivery === "unknown" ? "快捷键操作未确认，请查看诊断日志" : "快捷键操作失败，请查看诊断日志"); return; }
    // Receipt is not a successful session transition. Job/state events remain authoritative.
    const current = await this.deps.command(CMD.state, {});
    if (current.ok && current.data && typeof current.data === "object") this.deps.state(current.data as Record<string, unknown>);
    this.deps.notice("快捷键请求已提交");
  }
}
