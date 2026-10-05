/** Reusable file-route form. No IPC; settings and workspace share the same controls. */
import { h, on } from "../dom";
import { buildButton } from "./button";
import { tr } from "../i18n";
import { buildSelect } from "./controls";
import { buildField } from "./field";
import { replaceSelectOptions } from "./select-options";

export interface SpeechChoice { id: string; name: string; installed: boolean; runtimeAvailable?: boolean; externalRuntime?: string }
export function buildFileTranslationForm(onChange: (updates: Record<string, string>) => void, onRetry?: () => void) {
  const route = buildSelect("dual", [["dual", tr("识别＋翻译（双模型）")], ["single", tr("语音直接翻译（单模型）")]],
    value => onChange({ file_translation_mode: value }));
  route.setAttribute("aria-label", tr("文件翻译方式"));
  const model = buildSelect("", [], value => onChange({ speech_model_id: value }));
  model.setAttribute("aria-label", tr("语音翻译模型"));
  const field = buildField({ label: tr("语音翻译模型"), control: model });
  const device = buildSelect("auto", [["auto", tr("自动（优先 NVIDIA GPU）")], ["cuda", tr("NVIDIA GPU（CUDA）")], ["cpu", "CPU"]],
    value => onChange({ speech_device: value }));
  const output = buildSelect("bilingual", [["bilingual", tr("双语字幕")], ["translation", tr("仅译文")]],
    value => onChange({ speech_output: value }));
  const deviceField = buildField({ label: tr("语音翻译设备"), control: device });
  const outputField = buildField({ label: tr("字幕内容"), control: output });
  const note = h("p", { class: "field__hint", role: "status" });
  const retry = buildButton(tr("重新读取模型列表"), { small: true, hidden: true });
  on(retry, "click", () => { if (!retry.disabled) onRetry?.(); });
  const element = h("div", { class: "file-translation-form" }, [
    buildField({ label: tr("文件翻译方式"), control: route }), field, deviceField, outputField, note, retry,
  ]);
  let previous = "";
  const update = (config: Record<string, unknown>, models: SpeechChoice[], busy: boolean, error = false) => {
    const key = JSON.stringify([config["file_translation_mode"], config["speech_model_id"], config["speech_device"], config["speech_output"], models, busy, error]);
    if (previous === key) return;
    previous = key;
    const single = config["file_translation_mode"] === "single";
    route.value = single ? "single" : "dual";
    const id = String(config["speech_model_id"] ?? "speech-granite-4-1b");
    const installed = models.filter(item => item.installed && !item.externalRuntime);
    replaceSelectOptions(model, id, installed.map(item => [item.id, item.name] as const), tr("请先到模型广场下载语音翻译模型"));
    retry.hidden = !error || !onRetry;
    retry.disabled = busy;
    route.disabled = busy || error;
    model.disabled = busy || error || !installed.length;
    field.hidden = deviceField.hidden = outputField.hidden = !single;
    device.value = String(config["speech_device"] ?? "auto");
    output.value = String(config["speech_output"] ?? "bilingual");
    device.disabled = output.disabled = busy || error;
    const selected = installed.find(item => item.id === id);
    note.textContent = error ? tr("无法读取文件翻译设置，请稍后重试")
      : !single ? tr("沿用识别与翻译设置；实时和 OCR 配置不受影响。")
      : !selected ? tr("请先到模型广场下载语音翻译模型")
      : selected.runtimeAvailable === false ? tr("缺少语音翻译运行组件，请安装完整版本")
      : tr("仅用于文件字幕，不启动独立翻译器。自动模式优先 CUDA，缺少可用 CUDA 时使用 CPU；指定 GPU 失败不会偷偷切回 CPU。Granite 双语分两次生成，仅译文为一次。Index 官方建议约 10GB 显存并留余量。实际效果由你试用，实时和 OCR 配置不受影响。");
  };
  return { element, update };
}
