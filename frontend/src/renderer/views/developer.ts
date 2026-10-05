import { buildButton } from "../ui/button";
/** Developer diagnostics is a view, not a shell or privilege bypass. */
import { h, on } from "../dom";
import { call } from "../store";
import { CMD } from "../protocol";
import { tr } from "../i18n";
import { developerEnabled, setDeveloperEnabled, ipcSnapshot } from "../../shared/diagnostic-controls";
import type { PageHandle } from "../../shared/page-lifecycle";

export function buildDeveloperTab(onDisabled: () => void): PageHandle {
  const root = h("div", { class: "tab-page" });
  const actions = h("div", { class: "tuning-actions diagnostic-actions" });
  const output = h("pre", { class: "log-view" });
  const countdown = h("span", { class: "hint", text: tr("详细日志未开启") });
  let disposed = false;
  let expires = 0;
  let latest: Record<string, unknown> | null = null;
  let generation = 0;
  const refresh = async (environment = false): Promise<void> => {
    const request = ++generation;
    const data = await call<Record<string, unknown>>(CMD.diagnosticSnapshot, { environment });
    if (disposed || !developerEnabled() || request !== generation) return;
    if (!data) { output.textContent = tr("诊断快照获取失败"); return; }
    const overlaySurface = await window.voxsub?.overlay?.getGlass().catch(() => ({
      active: false, clippingCheck: "not_run", materialCheck: "not_run", desktopCheck: "not_run", reason: "unavailable",
    }));
    if (disposed || request !== generation) return;
    latest = { ...data, overlaySurface, ipcRenderer: ipcSnapshot(), rendererEnvironment: { userAgent: navigator.userAgent, platform: navigator.platform } };
    output.textContent = JSON.stringify(latest, null, 2);
    const session = data["verbose_session"] as { expires_at?: string } | null;
    expires = session?.expires_at ? Date.parse(session.expires_at) : 0;
  };
  const add = (label: string, action: () => void | Promise<void>): HTMLButtonElement => {
    const button = buildButton(tr(label));
    on(button, "click", () => void action()); actions.append(button); return button;
  };
  add("刷新运行状态", () => refresh());
  add("采集环境信息", () => refresh(true));
  add("开启详细日志（5 分钟）", async () => { await call(CMD.diagnosticSession, { action: "start", seconds: 300 }); if (!disposed) await refresh(); });
  add("结束详细日志", async () => { await call(CMD.diagnosticSession, { action: "stop" }); if (!disposed) { expires = 0; await refresh(); } });
  add("复制脱敏诊断快照", async () => {
    await refresh(true);
    if (!disposed && latest) {
      try { await navigator.clipboard.writeText(JSON.stringify(latest, null, 2)); }
      catch { countdown.textContent = tr("复制失败，请重试"); }
    }
  });
  add("关闭开发者模式", async () => {
    const result = await call<{ enabled: boolean }>(CMD.developerMode, { enabled: false });
    if (result?.enabled === false) { setDeveloperEnabled(false); expires = 0; if (!disposed) onDisabled(); }
  });
  root.append(h("p", { class: "hint", text: tr("仅本次启动有效。环境快照含设备型号、系统及驱动版本，不含序列号、用户名和正文。推理设备未验证时明确标注。") }), actions, countdown, output);
  const timer = setInterval(() => {
    if (disposed) return;
    const seconds = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
    countdown.textContent = seconds > 0 ? tr("详细日志剩余 {seconds} 秒").replace("{seconds}", String(seconds)) : tr("详细日志未开启");
  }, 1000);
  void refresh();
  return { element: root, dispose: () => { if (disposed) return; disposed = true; generation++; clearInterval(timer); } };
}
