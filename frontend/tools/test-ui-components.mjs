#!/usr/bin/env node
/**
 * 设置类字段与路径选择字段（SettingsField / PathPicker）—— 离线行为测试。
 *
 * ## 守的是什么
 *
 * 这两块表现在设置页出现 26 + 3 次，其中真正会出错的是**行为**而不是结构：
 *
 *   SettingsField
 *     · 控件被置灰时必须**同时**给出"被谁接管"的原因 —— 只变灰，用户会以为界面坏了；
 *     · 原因排在说明之前（先讲"为什么不能改"，再讲"这项是什么"）；
 *     · 组合控件（自带标签的开关）不得被再套一层 label，否则一屏两个标题。
 *
 *   PathPicker
 *     · **取消契约**：用户在系统对话框里点取消（`pick()` 返回空）时，
 *       绝不落库、绝不刷新 —— 否则点一次取消就把"自定义路径"静默写进配置；
 *     · 当前路径为空时「打开」按钮不发命令（发出去只会表现为"点了没反应"）；
 *     · 选中之后才交给回调落库。
 *
 * ## 怎么测的
 *
 * 用 `tools/mini-dom.mjs` 在 node 里渲染**真实的组件代码**，然后断言渲染出来的
 * DOM（类名/文案/禁用状态）与点击之后的回调结果。不启动 Electron、不弹系统
 * 对话框（picker 是注入的假函数）、不播放音频、不抢焦点。
 *
 * 用法：node tools/test-ui-components.mjs
 */
import { installMiniDom } from "./mini-dom.mjs";
import { importShared, createReporter } from "./esbuild-ts.mjs";

const dom = installMiniDom();
const { document, makeEvent } = dom;
const { check, finish } = createReporter("设置类字段与路径选择字段（§3.7 SettingsField / PathPicker）");

const [fieldMod, pickerMod] = await importShared(
  ["src/renderer/ui/field.ts", "src/renderer/ui/path-picker.ts"],
  { bundle: true },
);
const { buildField } = fieldMod;
const { buildPathPicker } = pickerMod;

/* ============================================================ SettingsField */

console.log("=== 字段：结构、说明与禁用原因 ===\n");
{
  const control = document.createElement("input");
  control.setAttribute("type", "text");
  control.setAttribute("value", "x");

  const wrap = buildField({ label: "语音灵敏度", control, hint: "越高越不容易把背景噪声当成说话" });
  document.body.append(wrap);

  check("容器是 div.field", wrap.tagName === "DIV" && wrap.classList.contains("field"), wrap.className);
  check("未禁用时没有 is-locked", !wrap.classList.contains("is-locked"));
  check("标签文案等于传入的 label", wrap.querySelector(".field__label")?.textContent === "语音灵敏度");
  check("控件就在字段里（没有被替换成副本）", wrap.querySelector("input") === control);
  check("说明渲染成 .field__hint", wrap.querySelector(".field__hint")?.textContent === "越高越不容易把背景噪声当成说话");
  check("没有禁用原因时只有一条提示", wrap.querySelectorAll(".field__hint").length === 1, String(wrap.querySelectorAll(".field__hint").length));
}

{
  const control = document.createElement("select");
  const wrap = buildField({
    label: "调优预设",
    control,
    hint: "该设置同时用于 A/B/C 三种模式",
    lockedBy: "仅「智能上下文」档可用",
  });
  document.body.append(wrap);

  check("给了禁用原因 → 容器带 is-locked", wrap.classList.contains("is-locked"), wrap.className);

  const notes = [...wrap.querySelectorAll(".field__hint")].map((n) => n.textContent);
  check("禁用原因与说明都在，共两条", notes.length === 2, JSON.stringify(notes));
  check("禁用原因排在**前**（先解释为什么不能改）", notes[0] === "仅「智能上下文」档可用", JSON.stringify(notes));
  check("说明排在其后", notes[1] === "该设置同时用于 A/B/C 三种模式", JSON.stringify(notes));
  // 顺序不是小事：反过来的话用户先读到"这项是什么"，要滑到最后才知道自己改不了
  const order = [...wrap.childNodes].map((n) => n.nodeName);
  check(
    "DOM 顺序 = 标签 → 控件 → 禁用原因 → 说明",
    order.join(",") === "LABEL,SELECT,P,P",
    order.join(","),
  );
}

{
  const wrap = buildField({ label: "无说明", control: document.createElement("span") });
  document.body.append(wrap);
  check("没有说明/原因时不渲染任何 .field__hint", wrap.querySelectorAll(".field__hint").length === 0);
}

{
  const wrap = buildField({ label: "只有禁用原因", control: document.createElement("span"), lockedBy: "当前档位下不可调整" });
  document.body.append(wrap);
  check("只有禁用原因时仍渲染它", wrap.querySelector(".field__hint")?.textContent === "当前档位下不可调整");
  check("且带 is-locked", wrap.classList.contains("is-locked"));
}

{
  // 组合控件（开关自带标签）：不能再套一个 label，否则出现两个标题
  const switchWrap = document.createElement("label");
  switchWrap.className = "switch";
  switchWrap.append(document.createElement("input"), document.createElement("span"));
  const box = buildField({ control: switchWrap, lockedBy: "仅「智能上下文」档可用" });
  document.body.append(box);

  check("不给 label 时不渲染 .field__label（避免两个标题）", box.querySelector(".field__label") === null);
  check("组合控件本体仍是原节点", box.querySelector(".switch") === switchWrap);
  check("置灰原因照常显示", box.querySelector(".field__hint")?.textContent === "仅「智能上下文」档可用");
  check("容器仍带 is-locked", box.classList.contains("is-locked"));
}

{
  // 空字符串等价于"没给"：不能因为传了 "" 就多出一条空提示行
  const wrap = buildField({ label: "空串", control: document.createElement("span"), hint: "", lockedBy: "" });
  document.body.append(wrap);
  check("hint/lockedBy 是空串时不渲染提示行", wrap.querySelectorAll(".field__hint").length === 0);
  check("空串 lockedBy 也不加 is-locked", !wrap.classList.contains("is-locked"));
}

{
  // 组件不吞控件自己的事件：传给它的 input 仍然照常触发 change
  const control = document.createElement("input");
  let changes = 0;
  control.addEventListener("change", () => { changes += 1; });
  const wrap = buildField({ label: "事件不被吞", control });
  document.body.append(wrap);
  control.dispatchEvent(makeEvent("change"));
  check("字段内的控件仍能收到自己的事件", changes === 1, `事件触发 ${changes} 次`);
}

/* ============================================================ PathPicker */

console.log("\n=== 路径选择：显示、打开、更改 ===\n");

/** 造一个假选择器：返回预设结果（不弹任何系统对话框）。 */
function fakePicker(result) {
  const calls = { pick: 0 };
  return {
    calls,
    pick: async () => {
      calls.pick += 1;
      return typeof result === "function" ? result() : result;
    },
  };
}

{
  const picked = [];
  const { pick } = fakePicker("/new/models");
  const handle = buildPathPicker({
    label: "当前位置",
    value: "/old/models",
    emptyText: "使用默认位置",
    hint: "模型保存在自定义位置。更新软件不会清空这个文件夹。",
    openLabel: "打开文件夹",
    changeLabel: "更改保存位置",
    openFor: () => "/old/models",
    onOpen: () => undefined,
    pick,
    onPicked: (path) => { picked.push(path); },
  });
  document.body.append(handle.field, handle.actions);

  check("字段带标签", handle.field.querySelector(".field__label")?.textContent === "当前位置");
  check("路径用只读样式展示", handle.field.querySelector(".readonly-value")?.textContent === "/old/models");
  check("字段说明照常显示", handle.field.querySelector(".field__hint")?.textContent?.startsWith("模型保存在自定义位置"));

  const buttons = handle.actions.querySelectorAll("button");
  check("按钮行有两个按钮", buttons.length === 2, String(buttons.length));
  check("第一个是「打开文件夹」", buttons[0]?.textContent === "打开文件夹");
  check("第二个是「更改保存位置」", buttons[1]?.textContent === "更改保存位置");

  // 点「更改」→ 选择器被调用 → 选中结果交给回调
  handle.actions.querySelectorAll("button")[1]?.click();
  await dom.flushAsync();
  check("选中的路径交给了 onPicked", picked.join(",") === "/new/models", JSON.stringify(picked));
  check("「更改」按钮确实触发了选择流程（只触发一次）", picked.length === 1, `选中 ${picked.length} 次`);
}

{
  // 取消（null）→ 绝不落库
  const picked = [];
  const { calls, pick } = fakePicker(null);
  const handle = buildPathPicker({
    label: "当前位置",
    value: "/old/cache",
    emptyText: "使用默认位置",
    openLabel: "打开缓存",
    changeLabel: "更改缓存位置",
    openFor: () => "/old/cache",
    onOpen: () => undefined,
    pick,
    onPicked: (path) => { picked.push(path); },
  });
  document.body.append(handle.field, handle.actions);

  handle.actions.querySelectorAll("button")[1]?.click();
  await dom.flushAsync();
  check("取消时选择器确实被调用了", calls.pick === 1, `${calls.pick} 次`);
  check("取消时 onPicked 一次都不调用（不会把取消写成自定义路径）", picked.length === 0, JSON.stringify(picked));
  check("取消时显示值不变", handle.field.querySelector(".readonly-value")?.textContent === "/old/cache");
}

{
  // 取消（空串 / undefined）→ 同样不落库
  for (const [label, result] of [["空串", ""], ["undefined", undefined]]) {
    const picked = [];
    const { pick } = fakePicker(result);
    const handle = buildPathPicker({
      label: "当前位置",
      value: "",
      emptyText: "使用默认位置",
      openLabel: "打开",
      changeLabel: "更改",
      openFor: () => "",
      onOpen: () => undefined,
      pick,
      onPicked: (path) => { picked.push(path); },
    });
    document.body.append(handle.field, handle.actions);
    check(`${label} 显示为占位文案`, handle.field.querySelector(".readonly-value")?.textContent === "使用默认位置");
    handle.actions.querySelectorAll("button")[1]?.click();
    await dom.flushAsync();
    check(`${label} 属于取消 → onPicked 不调用`, picked.length === 0);
  }
}

{
  // 「打开」：有目标就发，没目标不发
  const opened = [];
  const handle = buildPathPicker({
    label: "当前位置",
    value: "",
    emptyText: "使用默认位置",
    openLabel: "打开文件夹",
    changeLabel: "更改保存位置",
    // 显示值为空，但真实目标来自后端回传的路径 —— 这正是 openFor 存在的原因
    openFor: () => "/state/models-root",
    onOpen: (path) => { opened.push(path); },
    pick: async () => null,
    onPicked: () => undefined,
  });
  document.body.append(handle.field, handle.actions);
  handle.actions.querySelectorAll("button")[0]?.click();
  await dom.flushAsync();
  check("「打开」用的是 openFor 的目标（不是显示值）", opened.join(",") === "/state/models-root", JSON.stringify(opened));

  const noTarget = [];
  const handle2 = buildPathPicker({
    label: "当前位置",
    value: "",
    emptyText: "使用默认位置",
    openLabel: "打开文件夹",
    changeLabel: "更改保存位置",
    openFor: () => "",
    onOpen: (path) => { noTarget.push(path); },
    pick: async () => null,
    onPicked: () => undefined,
  });
  document.body.append(handle2.field, handle2.actions);
  handle2.actions.querySelectorAll("button")[0]?.click();
  await dom.flushAsync();
  check("没有可打开的位置时不发命令（避免「点了没反应」）", noTarget.length === 0);
}

{
  // setValue：选完之后立刻反映到界面
  const handle = buildPathPicker({
    label: "当前位置",
    value: "",
    emptyText: "使用默认位置",
    openLabel: "打开",
    changeLabel: "更改",
    openFor: () => "",
    onOpen: () => undefined,
    pick: async () => "/picked",
    onPicked: async (path) => { handle.setValue(path); },
  });
  document.body.append(handle.field, handle.actions);
  handle.actions.querySelectorAll("button")[1]?.click();
  await dom.flushAsync();
  check("onPicked 里 setValue 后界面显示新路径", handle.field.querySelector(".readonly-value")?.textContent === "/picked");

  handle.setValue("");
  check("setValue 传空 → 回落占位文案", handle.field.querySelector(".readonly-value")?.textContent === "使用默认位置");
}

{
  // 异步落库（生产代码里是 saveConfig，返回前界面不该先跳）
  const order = [];
  const handle = buildPathPicker({
    label: "当前位置",
    value: "/before",
    emptyText: "使用默认位置",
    openLabel: "打开",
    changeLabel: "更改",
    openFor: () => "",
    onOpen: () => undefined,
    pick: async () => { order.push("pick"); return "/after"; },
    onPicked: async (path) => {
      await new Promise((resolve) => setTimeout(resolve, 0));
      order.push(`saved:${path}`);
      handle.setValue(path);
    },
  });
  document.body.append(handle.field, handle.actions);
  handle.actions.querySelectorAll("button")[1]?.click();
  await dom.flushAsync();
  check("顺序是「选择 → 落库 → 显示新值」", order.join(" | ") === "pick | saved:/after", order.join(" | "));
  check("落库完成后显示新路径", handle.field.querySelector(".readonly-value")?.textContent === "/after");
}

/* ============================================================ 组件不依赖业务 */

{
  // 组件不得导入具体业务页面：源码里不能出现 views/、store、i18n 的引用
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const { ROOT } = await import("./esbuild-ts.mjs");
  const sources = ["src/renderer/ui/field.ts", "src/renderer/ui/path-picker.ts", "src/renderer/ui/progress.ts"];
  const bad = [];
  for (const file of sources) {
    const text = readFileSync(join(ROOT, file), "utf-8");
    for (const forbidden of ["views/", "../store", "../i18n", "protocol"]) {
      if (text.includes(forbidden)) bad.push(`${file}: ${forbidden}`);
    }
  }
  check("组件源码里没有业务页面/store/i18n 依赖", bad.length === 0, bad.join(", "));
}

dom.restore();
finish();
