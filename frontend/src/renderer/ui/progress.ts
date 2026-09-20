/**
 * 进度条（工作单 §3.7 的 JobProgress）。
 *
 * ## 抽的是什么
 *
 * 「轨道 + 填充 + 百分比标签」这套内部结构在本项目出现了四处
 * （C 模式文件进度、模型下载、迁移总进度、迁移每项进度），四处逐字重复
 * `div.progress__track > div.progress__fill` + `.progress__label`，并且各写一遍
 * "把百分比写进 style.width、把数字写进标签"。
 *
 * **只抽这条重复的内芯**：外层容器由各调用点自己决定
 * （`.progress` / `.cell__progress` / `.mig-progress__row`），标签类名也可覆盖
 * （模型目录里的标签是更小的 `.cell__progress-label`）。这样抽取不改变任何
 * 现有外观 —— 若把外层容器一起统一，反而会改动三处的排版。
 *
 * ## 百分比策略仍归调用点
 *
 * 三处对"总数为 0 / 未知"的处理并不一致（工作区显示「—」、模型目录显示「—」、
 * 迁移按 100% 处理）。统一它等于**改动现有显示**，所以这里不做决定：
 * `setPercent(null)` 明确表示"进度未知"，只不动宽度，由调用点自己决定标签写什么。
 *
 * ## 状态
 *
 * `setState` 只加状态类名（`is-running` / `is-done` / `is-failed`），不写文案 ——
 * 文案（完成 / 失败 / 进行中）由调用方经 `tr()` 给出。当前 app.css 没有这几条
 * 规则，因此外观不变；它让"失败/完成"在 DOM 上可断言，也让后续要给失败态上色时
 * 有一处统一的挂点。产品目前没有任何取消入口，所以这里**不发明**"取消中"状态。
 */
import { h } from "../dom";

export type ProgressBarState = "idle" | "running" | "done" | "failed";

const STATE_CLASS: Record<ProgressBarState, string> = {
  idle: "is-idle",
  running: "is-running",
  done: "is-done",
  failed: "is-failed",
};

export interface ProgressBarOptions {
  /** 标签元素的类名，默认 `progress__label`。 */
  labelClass?: string | undefined;
  /** 标签初始文案，默认为空。 */
  labelText?: string | undefined;
}

export interface ProgressBar {
  /** `div.progress__track`（内含 fill）。 */
  readonly track: HTMLElement;
  /** `div.progress__fill`。 */
  readonly fill: HTMLElement;
  /** 百分比标签。 */
  readonly label: HTMLElement;
  /** 写入百分比；`null` 表示进度未知，不动宽度。 */
  setPercent(percent: number | null): void;
  /** 写入标签文案。 */
  setLabel(text: string): void;
  /** 切换状态类名。 */
  setState(state: ProgressBarState): void;
}

export function buildProgressBar(options: ProgressBarOptions = {}): ProgressBar {
  const fill = h("div", { class: "progress__fill" });
  const track = h("div", { class: "progress__track" }, [fill]);
  const label = h("span", {
    class: options.labelClass ?? "progress__label",
    text: options.labelText ?? "",
  });

  return {
    track,
    fill,
    label,
    setPercent(percent: number | null): void {
      // 未知进度：保持原宽度（原先传进去的是 "—"，浏览器同样会丢弃这个非法宽度）
      if (percent === null || !Number.isFinite(percent)) return;
      const clamped = Math.max(0, Math.min(100, percent));
      fill.setAttribute("style", `width:${Math.round(clamped)}%`);
    },
    setLabel(text: string): void {
      label.textContent = text;
    },
    setState(state: ProgressBarState): void {
      for (const name of Object.values(STATE_CLASS)) track.classList.remove(name);
      track.classList.add(STATE_CLASS[state]);
    },
  };
}
