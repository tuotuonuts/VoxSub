/** Settings-only installed catalog. Refreshing never reloads config or rewrites drafts. */
import { h, on } from "../dom";
import { tr } from "../i18n";
import { call } from "../store";
import { CMD, type ModelEntry } from "../protocol";
import { buildSelect } from "../ui/controls";
import { replaceSelectOptions } from "../ui/select-options";
import { buildField } from "../ui/field";
import { buildButton } from "../ui/button";

type IsCurrent = () => boolean;

export class SettingsModelCatalog {
  private models: ModelEntry[] = [];
  private loading = false;
  private failed = false;
  private revision = 0;
  private fields: Array<() => void> = [];

  clearFields(): void { this.fields = []; }

  async refresh(isCurrent: IsCurrent = () => true): Promise<void> {
    const request = ++this.revision;
    this.loading = true;
    this.failed = false;
    this.updateFields();
    let result: { models: ModelEntry[] } | null = null;
    try { result = await call(CMD.listModels, { models_root: null }); }
    catch { /* call records IPC failures; preserve the last list and show unavailable. */ }
    if (request !== this.revision || !isCurrent()) return;
    this.loading = false;
    this.failed = !result || !Array.isArray(result.models);
    if (!this.failed && result) this.models = result.models;
    this.updateFields();
  }

  private updateFields(): void { this.fields.forEach(update => update()); }

  field(label: string, task: string, currentId: string, onPick: (id: string) => void,
    emptyHint: string, isCurrent: IsCurrent): HTMLElement {
    let desired = currentId;
    const select = buildSelect<string>("", [], value => {
      if (!isCurrent() || select.disabled || !value) return;
      desired = value;
      onPick(value);
    });
    select.setAttribute("aria-label", label);
    const note = h("p", { class: "field__hint", role: "status", "aria-live": "polite" });
    const retry = buildButton(tr("重新读取模型列表"), { small: true });
    on(retry, "click", () => { if (isCurrent()) void this.refresh(isCurrent); });
    const control = h("div", { class: "model-picker", "data-model-task": task }, [select, note, retry]);
    const update = (): void => {
      if (!isCurrent()) return;
      const available = this.models.filter(model => model.task === task && model.installed);
      const placeholder = desired ? tr("所选模型暂不可用，请重新选择") : tr("请选择模型");
      replaceSelectOptions(select, desired, available.map(model => [model.id, model.name] as const), placeholder);
      select.disabled = this.loading || this.failed || available.length === 0;
      select.setAttribute("aria-busy", String(this.loading));
      const missing = !!desired && !available.some(model => model.id === desired);
      note.textContent = this.failed ? tr("模型列表读取失败，请重试；已有设置未更改。")
        : this.loading ? tr("正在读取已安装模型…")
        : available.length === 0 ? emptyHint
        : missing ? placeholder : tr("只列出已下载到本机的模型");
      note.classList.toggle("field__hint--warn", this.failed || (!this.loading && (missing || available.length === 0)));
      retry.hidden = !this.failed;
      retry.disabled = this.loading;
    };
    this.fields.push(update);
    update();
    return buildField({ label, control });
  }
}
