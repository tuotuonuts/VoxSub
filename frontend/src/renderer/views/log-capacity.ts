/** Explicit transaction: unit/value changes do not prune logs until Save confirms. */
import { h, on } from "../dom";
import { tr } from "../i18n";
import { logBudgetMB } from "../../shared/diagnostic-controls";
export function buildLogCapacity(config: Record<string, unknown>, save: (updates: Record<string, unknown>) => Promise<void>): HTMLElement {
  const root = h("section", { class: "card" });
  root.append(h("h3", { class: "card__title", text: tr("日志存储上限") }));
  const body = h("div", { class: "card__body" });
  const unit = h("select", { class: "select", "aria-label": tr("容量单位") });
  for (const name of ["MB", "GB"]) unit.append(h("option", { value: name, text: name }));
  unit.value = String(config["log_limit_unit"] ?? "MB");
  let currentMB = Number(config["log_limit_mb"] ?? 50);
  const value = h("input", { class: "input", type: "number", step: "any", "aria-label": tr("日志存储上限"), value: String(currentMB / (unit.value === "GB" ? 1024 : 1)) });
  let previousUnit = unit.value;
  on(unit, "change", () => { const mb = Number(value.value) * (previousUnit === "GB" ? 1024 : 1); value.value = String(mb / (unit.value === "GB" ? 1024 : 1)); previousUnit = unit.value; });
  const status = h("p", { class: "hint" });
  const button = h("button", { type: "button", class: "btn btn--primary", text: tr("保存") });
  on(button, "click", async () => {
    const mb = logBudgetMB(Number(value.value), unit.value);
    if (mb === null) { status.textContent = tr("日志容量范围：10 MB 至 10 GB"); return; }
    if (mb < currentMB && !window.confirm(tr("降低日志上限可能删除最旧的轮转日志，不影响模型、配置或历史。继续？"))) return;
    button.disabled = true; value.disabled = true; unit.disabled = true;
    try { await save({ log_limit_mb: mb, log_limit_unit: unit.value }); currentMB = mb; status.textContent = tr("已保存"); }
    catch { status.textContent = tr("保存失败"); }
    finally { button.disabled = false; value.disabled = false; unit.disabled = false; }
  });
  body.append(h("p", { class: "hint", text: tr("MB/GB 按 1024 换算。上限管理应用轮转日志，最旧记录自动淘汰；不包含导出报告、模型和历史。") }), value, unit, button, status);
  root.append(body);
  return root;
}
