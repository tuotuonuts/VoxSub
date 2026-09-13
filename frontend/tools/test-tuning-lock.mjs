#!/usr/bin/env node
/**
 * 识别调优分页的置灰行为测试。
 *
 * ## 覆盖的需求
 *
 * 「使用智能上下文的时候一些不应该被微调的选项应该变灰不能被改动」
 *
 * 后端只有部分参数在任何档位都生效。选了预设档还去调那些被预设覆盖的参数，
 * 改了等于没改 —— 所以它们要置灰。逐条核对过后端调用点：
 *
 *   · 语音灵敏度 / 停顿多久断句 / 单句最长时长 / 识别候选数
 *     预设档下由 ASR_TUNING_PRESETS 覆盖，只有「自定义」档读用户值。
 *   · 上下文最长等待 / 实时双语草稿 / 上下文保守纠偏 / 语气词清理
 *     只作用于 ContextualTextProcessor，而它仅在「智能上下文」档创建。
 *   · 常用词 / 单句最大文字量 有非上下文的生效路径，任何时候都可改。
 *
 * ## 不碰用户配置
 *
 * 测试只切换档位、不点保存，所以配置不会被改写；结束时切回原本的档位。
 *
 * 用法：node tools/test-tuning-lock.mjs
 */
const PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const main = targets.find((t) => (t.title ?? "").includes("语幕"));
if (!main) { console.error("找不到主窗（应用需以 --debug 启动）"); process.exit(1); }

const socket = new WebSocket(main.webSocketDebuggerUrl);
let id = 1;
const pending = new Map();
socket.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  const r = pending.get(m.id);
  if (r) { pending.delete(m.id); r(m.result); }
});
await new Promise((r) => socket.addEventListener("open", r));
const send = (method, params) =>
  new Promise((r) => { const i = id++; pending.set(i, r); socket.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr, timeoutMs = 30000) => {
  const res = await Promise.race([
    send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), timeoutMs)),
  ]);
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description);
  return res.result.value;
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

/* ------------------------------------------------------------ 页面助手 */

/** 打开设置页的「识别调优」分页。 */
const openTuning = () => ev(`(async () => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await new Promise(r => setTimeout(r, 300));
  const back = document.querySelector('.page__back');
  if (back) { back.click(); await new Promise(r => setTimeout(r, 300)); }
  [...document.querySelectorAll('.topbar__actions button')]
    .find(b => b.textContent === '设置')?.click();
  await new Promise(r => setTimeout(r, 2500));
  [...document.querySelectorAll('.settings__tab')]
    .find(t => t.textContent === '识别调优')?.click();
  await new Promise(r => setTimeout(r, 1200));
})()`);

/**
 * 读某个字段的置灰状态。
 *
 * 两类控件结构不同：
 *   · 数字/下拉 —— .field > .field__label + input/select
 *   · 开关      —— .switch > .switch__label（置灰时外层包 .field.is-locked）
 */
const readField = (label) => ev(`(() => {
  const panes = document.querySelector('.settings__panes');
  if (!panes) return JSON.stringify({ missing: true });

  // 先按 .field__label 找（数字输入、下拉）
  for (const f of panes.querySelectorAll('.field')) {
    const lab = f.querySelector('.field__label')?.textContent.trim();
    if (lab !== ${JSON.stringify(label)}) continue;
    const ctl = f.querySelector('input, select');
    if (!ctl) continue;
    return JSON.stringify({
      kind: 'field',
      disabled: ctl.disabled === true,
      value: ctl.value,
      locked: f.classList.contains('is-locked'),
      notes: [...f.querySelectorAll('.field__hint')].map(n => n.textContent.trim()),
    });
  }

  // 再按 .switch__label 找（开关）
  for (const sw of panes.querySelectorAll('.switch')) {
    const lab = sw.querySelector('.switch__label')?.textContent.trim();
    if (lab !== ${JSON.stringify(label)}) continue;
    const ctl = sw.querySelector('input');
    const box = sw.closest('.field');
    return JSON.stringify({
      kind: 'switch',
      disabled: ctl?.disabled === true,
      value: String(ctl?.checked),
      locked: Boolean(box?.classList.contains('is-locked')),
      notes: box ? [...box.querySelectorAll('.field__hint')].map(n => n.textContent.trim()) : [],
    });
  }

  return JSON.stringify({ missing: true });
})()`);

/** 切换档位（不点保存，因此不会写配置）。 */
const switchProfile = (value) => ev(`(async () => {
  const panes = document.querySelector('.settings__panes');
  const sel = panes.querySelector('select');
  if (!sel) return JSON.stringify({ error: '找不到档位下拉' });
  sel.value = ${JSON.stringify(value)};
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 900));
  return JSON.stringify({ ok: true, now: sel.value });
})()`);

/* ------------------------------------------------------------ 前置 */

const original = await ev(`(async () => {
  const r = await window.voxsub.backend.command('get_config', null);
  return String(r?.data?.asr_tuning_profile ?? 'context');
})()`);
console.log(`配置里原本的档位：${original}\n`);

const BASE = ["语音灵敏度", "停顿多久断句", "单句最长时长", "识别候选数"];
const CONTEXT = ["上下文最长等待", "实时双语草稿", "上下文保守纠偏", "语气词清理"];
const ALWAYS = ["常用词 / 专有名词", "单句最大文字量"];

await openTuning();

/* ------------------------------------------------------------ 智能上下文 */

console.log("=== 「智能上下文」档 ===\n");
console.log("  基础参数由预设覆盖 → 应置灰；上下文参数生效 → 应可改。\n");
{
  const sw = JSON.parse(await switchProfile("context"));
  check("能切到智能上下文档", sw.ok === true, JSON.stringify(sw));

  for (const label of BASE) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」置灰`, d.disabled === true, `disabled=${d.disabled}`);
    check(`「${label}」显示预设值`, d.value !== "" && d.value != null, `value=${d.value}`);
    check(
      `「${label}」说明了置灰原因`,
      (d.notes ?? []).some((n) => n.includes("预设") || n.includes("自定义")),
      JSON.stringify(d.notes),
    );
  }

  for (const label of CONTEXT) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」可改`, d.disabled === false, `disabled=${d.disabled}`);
  }

  for (const label of ALWAYS) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」可改（不受档位影响）`, d.disabled === false, `disabled=${d.disabled}`);
  }

  // 预设值必须与后端报告一致 —— 显示一个不生效的数字比置灰更糟
  const reported = JSON.parse(await ev(`(async () => {
    const r = await window.voxsub.backend.command('asr_tuning_meta', null);
    return JSON.stringify(r?.data?.effective ?? {});
  })()`));
  const vad = JSON.parse(await readField("语音灵敏度"));
  check(
    "语音灵敏度显示的是生效值",
    String(reported["asr_vad_threshold"]) === String(vad.value),
    `界面 ${vad.value} / 后端 ${reported["asr_vad_threshold"]}`,
  );
  const beam = JSON.parse(await readField("识别候选数"));
  check(
    "识别候选数显示的是生效值",
    String(reported["asr_beam_paths"]) === String(beam.value),
    `界面 ${beam.value} / 后端 ${reported["asr_beam_paths"]}`,
  );
}

/* ------------------------------------------------------------ 快档 */

console.log("\n=== 「快档」档 ===\n");
console.log("  预设档下上下文参数无处可用 → 也应置灰。\n");
{
  const sw = JSON.parse(await switchProfile("responsive"));
  check("能切到快档", sw.ok === true, JSON.stringify(sw));

  for (const label of BASE) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」置灰`, d.disabled === true, `disabled=${d.disabled}`);
  }
  for (const label of CONTEXT) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」置灰（仅智能上下文档可用）`, d.disabled === true, `disabled=${d.disabled}`);
    check(
      `「${label}」说明了原因`,
      (d.notes ?? []).some((n) => n.includes("智能上下文")),
      JSON.stringify(d.notes),
    );
  }
  for (const label of ALWAYS) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」仍可改`, d.disabled === false, `disabled=${d.disabled}`);
  }
}

/* ------------------------------------------------------------ 自定义 */

console.log("\n=== 「自定义」档 ===\n");
console.log("  基础参数此时真正生效 → 应可改；上下文参数仍不可用。\n");
{
  const sw = JSON.parse(await switchProfile("custom"));
  check("能切到自定义档", sw.ok === true, JSON.stringify(sw));

  for (const label of BASE) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」可改`, d.disabled === false, `disabled=${d.disabled}`);
  }
  for (const label of CONTEXT) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」置灰`, d.disabled === true, `disabled=${d.disabled}`);
  }
  for (const label of ALWAYS) {
    const d = JSON.parse(await readField(label));
    check(`「${label}」仍可改`, d.disabled === false, `disabled=${d.disabled}`);
  }
}

/* ------------------------------------------------------------ 复位 */

console.log("\n=== 复位 ===");
{
  const sw = JSON.parse(await switchProfile(original));
  check(`切回原本的档位（${original}）`, sw.ok === true, JSON.stringify(sw));

  // 确认配置没被改动（测试只切换档位、不点保存）
  const after = await ev(`(async () => {
    const r = await window.voxsub.backend.command('get_config', null);
    return String(r?.data?.asr_tuning_profile ?? '');
  })()`);
  check("配置未被测试改写", after === original, `配置 ${after} / 原本 ${original}`);
}

socket.close();

const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
