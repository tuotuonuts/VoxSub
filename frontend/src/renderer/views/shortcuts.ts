/** Explicit-save shortcut settings. Conflict probes do not mutate saved registrations. */
import { h, on } from "../dom";
import { tr } from "../i18n";
import { buildCard } from "../ui/card";
import { buildButton } from "../ui/button";
import { buildShortcutField, type ShortcutField } from "../ui/shortcut-field";
import { PageLifecycle, type PageHandle } from "../../shared/page-lifecycle";
import { emptyBindings, SHORTCUT_ACTIONS, SHORTCUT_LABELS, validateBindings, type ShortcutIssue, type ShortcutSnapshot } from "../../shared/shortcuts";
function issueText(issue: ShortcutIssue, action?: string): string {
  const other = action === issue.other ? issue.action : issue.other;
  if (issue.code === "duplicate") return tr("与“{action}”重复，请更换组合键").replace("{action}", other ? tr(SHORTCUT_LABELS[other]) : tr("其他操作"));
  return tr({ invalid: "组合键格式无效，请使用 Ctrl 或 Alt 加一个按键", reserved: "这是系统保留组合键，请更换", occupied: "系统无法注册此组合键，可能已被占用或被系统保留", storage: "快捷键设置无法读取或保存，请检查文件权限后重试", capturing: "请先结束快捷键录入" }[issue.code]);
}
export function buildShortcutSettings(): PageHandle {
  const lifecycle = new PageLifecycle();
  const root = h("div", { class: "tab-page" });
  const api = window.voxsub?.shortcuts;
  const status = h("p", { class: "hint", "aria-live": "polite" });
  const fields = new Map<string, ShortcutField>();
  let snapshot: ShortcutSnapshot | null = null, draft = emptyBindings(), issues: ShortcutIssue[] = [];
  let dirty = false, working = false, revision = 0;
  let checkTimer: ReturnType<typeof setTimeout> | null = null;
  const save = buildButton(tr("保存快捷键"), { variant: "primary", disabled: true });
  const reset = buildButton(tr("放弃更改")); const clearAll = buildButton(tr("清空全部快捷键"));
  const renderStatus = (): void => {
    if (lifecycle.disposed) return;
    const normalized = validateBindings(draft).bindings;
    for (const action of SHORTCUT_ACTIONS) {
      const field = fields.get(action)!;
      const issue = issues.find(item => item.action === action || item.other === action);
      field.setStatus(issue ? issueText(issue, action) : !draft[action] ? tr("未设置") : snapshot?.bindings[action] === normalized[action] && snapshot?.active.includes(action) ? tr("已启用") : tr("尚未保存"));
      field.setDisabled(working || !snapshot);
    }
    save.disabled = working || !snapshot || !!snapshot.capturing || [...fields.values()].some(field => field.isCapturing);
    reset.disabled = save.disabled; clearAll.disabled = save.disabled;
  };
  const check = (): void => {
    const current = ++revision;
    if (checkTimer) clearTimeout(checkTimer);
    issues = validateBindings(draft).issues; renderStatus();
    if (!api || issues.length || snapshot?.capturing) return;
    checkTimer = setTimeout(() => {
      checkTimer = null;
      void api.check({ ...draft }).then(result => {
        if (lifecycle.disposed || current !== revision) return;
        issues = result?.issues ?? [{ code: "storage" }]; renderStatus();
      }).catch(() => { if (!lifecycle.disposed && current === revision) { status.textContent = tr("快捷键检查未确认，请重试"); } });
    }, 160);
  };
  const body = SHORTCUT_ACTIONS.map(action => {
    const field = buildShortcutField({ label: tr(SHORTCUT_LABELS[action]), value: "", translate: tr,
      changed: value => { draft[action] = value; dirty = true; status.textContent = tr("更改尚未保存"); check(); },
      beginCapture: token => api?.beginCapture(token) ?? Promise.resolve(false),
      endCapture: token => api?.endCapture(token) ?? Promise.resolve(),
      captureChanged: () => renderStatus(),
    }); fields.set(action, field); lifecycle.add(() => field.dispose()); return field.element;
  });
  root.append(buildCard(tr("全局快捷键"), [
    h("p", { class: "hint", text: tr("默认全部留空。设置后，即使应用最小化或位于托盘，快捷键也可生效。") }),
    h("p", { class: "hint", text: tr("可点击录入快捷键，也可输入 Ctrl+Alt+T 等组合。清空后保存即可停用。") }),
    ...body,
    h("p", { class: "hint", text: tr("自动检查应用内重复及系统注册冲突。其他软件仅在自身窗口内使用的快捷键无法可靠检测，请避免常用编辑组合键。") }),
    h("p", { class: "hint", text: tr("会话操作使用当前模式、音源和模型；录音切换遵循当前音源和会话状态限制，不自动启动录音。OCR 操作不使用这些会话快捷键。") }),
  ]), h("div", { class: "tuning-actions" }, [save, reset, clearAll]), status);
  const receive = (data: ShortcutSnapshot): void => {
    if (lifecycle.disposed || !data) return;
    const captureEnded = snapshot?.capturing && !data.capturing;
    snapshot = data;
    if (captureEnded) for (const field of fields.values()) field.cancelCapture();
    if (!dirty) { draft = { ...data.bindings }; for (const action of SHORTCUT_ACTIONS) fields.get(action)!.setValue(draft[action]); issues = data.issues; }
    const globalIssue = data.issues.find(issue => !issue.action);
    if (globalIssue) status.textContent = issueText(globalIssue);
    renderStatus();
  };
  if (api) {
    lifecycle.add(api.onChanged(receive));
    void api.get().then(data => { if (data) receive(data); else if (!lifecycle.disposed) status.textContent = tr("快捷键功能暂不可用"); }).catch(() => { if (!lifecycle.disposed) status.textContent = tr("快捷键功能暂不可用"); });
  } else status.textContent = tr("快捷键功能暂不可用");
  on(save, "click", () => {
    if (!api || working || !snapshot || snapshot.capturing || [...fields.values()].some(field => field.isCapturing)) return;
    working = true; revision++; if (checkTimer) { clearTimeout(checkTimer); checkTimer = null; } renderStatus();
    void api.save({ ...draft }).then(result => {
      if (lifecycle.disposed) return;
      if (result?.ok) { dirty = false; issues = []; receive(result.snapshot); status.textContent = tr("快捷键已保存并生效"); }
      else { issues = result?.issues ?? [{ code: "storage" }]; if (result) snapshot = result.snapshot; status.textContent = tr("快捷键未保存，请处理冲突或错误"); }
    }).catch(() => { if (!lifecycle.disposed) status.textContent = tr("快捷键保存未确认，请重新打开设置核对"); })
      .finally(() => { working = false; renderStatus(); });
  });
  on(reset, "click", () => { if (!snapshot || working) return; dirty = false; revision++; receive(snapshot); status.textContent = tr("已放弃快捷键更改"); });
  on(clearAll, "click", () => { if (working) return; draft = emptyBindings(); dirty = true; for (const field of fields.values()) field.setValue(""); status.textContent = tr("更改尚未保存"); check(); });
  lifecycle.add(() => { revision++; if (checkTimer) clearTimeout(checkTimer); });
  renderStatus();
  return { element: root, dispose: () => lifecycle.dispose() };
}
