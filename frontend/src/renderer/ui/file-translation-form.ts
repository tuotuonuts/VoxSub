/** Reusable file-route form. No IPC; settings and workspace share the same controls. */
import { h, on } from "../dom";
import { buildButton } from "./button";
import { tr } from "../i18n";
import { buildSelect } from "./controls";
import { buildField } from "./field";
import { replaceSelectOptions } from "./select-options";

export interface SpeechChoice { id: string; name: string; installed: boolean; runtimeAvailable?: boolean }
export function buildFileTranslationForm(onChange: (updates: Record<string, string>) => void, onRetry?: () => void) {
  const route = buildSelect("dual", [["dual", tr("识别＋翻译（双模型）")], ["single", tr("语音直接翻译（单模型）")]],
    value => onChange({ file_translation_mode: value }));
  route.setAttribute("aria-label", tr("文件翻译方式"));
  const model = buildSelect("", [], value => onChange({ speech_model_id: value }));
  model.setAttribute("aria-label", tr("语音翻译模型"));
  const field = buildField({ label: tr("语音翻译模型"), control: model });
  const note = h("p", { class: "field__hint", role: "status" });
  const retry = buildButton(tr("重新读取模型列表"), { small: true, hidden: true });
  on(retry, "click", () => { if (!retry.disabled) onRetry?.(); });
  const element = h("div", { class: "file-translation-form" }, [
    buildField({ label: tr("文件翻译方式"), control: route }), field, note, retry,
  ]);
  let previous = "";
  const update = (config: Record<string, unknown>, models: SpeechChoice[], busy: boolean, error = false) => {
    const key = JSON.stringify([config["file_translation_mode"], config["speech_model_id"], models, busy, error]);
    if (previous === key) return;
    previous = key;
    const single = config["file_translation_mode"] === "single";
    route.value = single ? "single" : "dual";
    const id = String(config["speech_model_id"] ?? "speech-granite-4-1b");
    const installed = models.filter(item => item.installed);
    replaceSelectOptions(model, id, installed.map(item => [item.id, item.name] as const), tr("请先到模型广场下载语音翻译模型"));
    retry.hidden = !error || !onRetry;
    retry.disabled = busy;
    route.disabled = busy || error;
    model.disabled = busy || error || !installed.length;
    field.hidden = !single;
    const selected = installed.find(item => item.id === id);
    note.textContent = error ? tr("无法读取文件翻译设置，请稍后重试")
      : !single ? tr("沿用识别与翻译设置；实时和 OCR 配置不受影响。")
      : !selected ? tr("请先到模型广场下载语音翻译模型")
      : selected.runtimeAvailable === false ? tr("缺少语音翻译运行组件，请安装完整版本")
      : tr("仅用于文件字幕。不启动独立翻译器、不录音、不朗读。CPU 处理可能较慢；Granite 原文和译文分两次生成，时间轴为分段范围。切换到实时或 OCR 会恢复原来的双模型配置。");
  };
  return { element, update };
}
