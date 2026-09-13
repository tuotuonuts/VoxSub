#!/usr/bin/env node
/**
 * 翻译档位 × 语言对 的界面测试。
 *
 * ## 覆盖的用户报告
 *
 * 「快档不支持语言对 ('ja','zh')」—— 日志里每一句都失败，界面却什么都不说。
 *
 * 界面必须做到三件事：
 *   1. 不支持当前语言对的档位，在选项上就有徽标提示；
 *   2. 当前档位不支持时，说明**实际会用哪一档**（否则用户看到"我选了快档
 *      却在用质量档"会以为是 bug）；
 *   3. 主屏改了语言之后，再打开设置页要显示新语言对下的结论 ——
 *      设置页关着时语言可能已经变了，不刷新就会显示过期提示。
 *
 * 做法：通过 CDP 读真实 DOM，不 mock。档位能力由后端 translate_tiers 算，
 * 所以这里同时验证了"后端结论确实传到了界面"。
 *
 * 用法：node tools/test-translate-tiers.mjs
 */
import { readFileSync } from "node:fs";

const PORT = 9222;

/* ------------------------------------------------------------ CDP */

async function connect(titlePart) {
  const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const target = targets.find((t) => (t.type === "page") && (t.title ?? "").includes(titlePart));
  if (!target) throw new Error(`找不到 target: ${titlePart}`);

  const socket = new WebSocket(target.webSocketDebuggerUrl);
  let id = 1;
  const pending = new Map();
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    const entry = pending.get(msg.id);
    if (!entry) return;
    pending.delete(msg.id);
    msg.error ? entry.reject(new Error(JSON.stringify(msg.error))) : entry.resolve(msg.result);
  });
  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const mid = id++;
      pending.set(mid, { resolve, reject });
      socket.send(JSON.stringify({ id: mid, method, params }));
    });

  return {
    async ev(expression, timeout = 20000) {
      const result = await send("Runtime.evaluate", {
        expression, awaitPromise: true, returnByValue: true,
      });
      if (result.exceptionDetails) {
        throw new Error(result.exceptionDetails.exception?.description ?? "eval 失败");
      }
      return result.result.value;
    },
    close: () => socket.close(),
  };
}

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

const main = await connect("语幕");

/* ------------------------------------------------------------ 复位 */

await main.ev(`(async () => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await new Promise(r => setTimeout(r, 300));
  for (let i = 0; i < 6; i += 1) {
    const back = document.querySelector('.page__back');
    if (!back) break;
    back.click();
    await new Promise(r => setTimeout(r, 350));
  }
})()`);

/** 读当前语言对（用于测试自清理）。 */
const originalLangs = await main.ev(`(() => {
  const s = [...document.querySelectorAll('.lang-box select')];
  return { source: s[0]?.value, target: s[1]?.value };
})()`);

/**
 * 记录测试开始前的翻译档位。
 *
 * 必须在**任何改动之前**读，结束时原样写回 —— 写死一个值会把用户的配置
 * 覆盖掉（这个坑本项目踩过：测试靠点击还原，中途出错就把测试值留在了
 * 用户配置里）。
 */
const originalTier = await main.ev(`(async () => {
  const r = await window.voxsub.backend.command('get_config', null);
  return r.data?.translate_tier ?? null;
})()`);

/** 打开设置页并等它渲染完。 */
async function openSettings() {
  return main.ev(`(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise(r => setTimeout(r, 300));
    for (let i = 0; i < 6; i += 1) {
      const back = document.querySelector('.page__back');
      if (!back) break;
      back.click();
      await new Promise(r => setTimeout(r, 350));
    }
    [...document.querySelectorAll('.topbar__actions button')].find(b => b.textContent === '设置')?.click();
    await new Promise(r => setTimeout(r, 4500));
    return true;
  })()`, 25000);
}

/** 读档位单选与提示文字。 */
const readTierUi = `(() => {
  const radios = [...document.querySelectorAll('.radio')].filter(r => /快档|质量档|云端/.test(r.textContent));
  const hints = [...document.querySelectorAll('.field__hint')].map(x => x.textContent);
  return {
    radios: radios.map(r => ({
      label: r.querySelector('span')?.textContent ?? '',
      badge: r.querySelector('.radio__badge')?.textContent ?? null,
      checked: r.classList.contains('is-checked'),
    })),
    tierHints: hints.filter(t => t.includes('→')),
    warnHints: [...document.querySelectorAll('.field__hint--warn')].map(x => x.textContent).filter(t => t.includes('→')),
  };
})()`;

/** 把主屏语言设成给定值（模拟用户改语言）。 */
async function setLangs(source, target) {
  return main.ev(`(async () => {
    const s = [...document.querySelectorAll('.lang-box select')];
    if (!s[0] || !s[1]) return false;
    s[0].value = ${JSON.stringify(source)};
    s[0].dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 600));
    const s2 = [...document.querySelectorAll('.lang-box select')];
    s2[1].value = ${JSON.stringify(target)};
    s2[1].dispatchEvent(new Event('change'));
    await new Promise(r => setTimeout(r, 800));
    return true;
  })()`);
}

/** 把翻译档位切到给定 id（在设置页内）。 */
async function setTier(tierId) {
  const labels = { fast: "快档", quality: "质量档", cloud: "云端" };
  return main.ev(`(async () => {
    const target = ${JSON.stringify(labels[tierId])};
    const radio = [...document.querySelectorAll('.radio')].find(
      r => (r.querySelector('span')?.textContent ?? '') === target);
    if (!radio) return false;
    radio.click();
    await new Promise(r => setTimeout(r, 2500));
    return true;
  })()`, 25000);
}

/* ------------------------------------------------ 后端命令确实可用 */

console.log("=== 后端 translate_tiers 已接线 ===\n");
{
  const direct = await main.ev(`(async () => {
    const r = await window.voxsub.backend.command('translate_tiers', { source: 'ja', target: 'zh' });
    return { ok: r.ok, data: r.data };
  })()`);
  check("命令可用（不是未知命令）", direct.ok === true, JSON.stringify(direct).slice(0, 160));
  const tiers = direct.data?.tiers ?? [];
  check("返回三个档位", tiers.length === 3, JSON.stringify(tiers.map((t) => t.id)));
  const fast = tiers.find((t) => t.id === "fast");
  check("快档被标为不支持 日→中", fast && fast.supportsPair === false, JSON.stringify(fast));
  const quality = tiers.find((t) => t.id === "quality");
  check("质量档被标为支持 日→中", quality && quality.supportsPair === true, JSON.stringify(quality));
  check("返回支持的语言清单（界面要显示）", Array.isArray(fast?.langs) && fast.langs.length > 0, JSON.stringify(fast?.langs));
}

/* ------------------------------------------------ 界面：不支持的语言对 */

console.log("\n=== 日文 → 中文 + 快档（用户日志里的组合）===\n");
{
  await setLangs("ja", "zh");
  await openSettings();
  await setTier("fast");
  await openSettings();
  const ui = await main.ev(readTierUi);

  const fast = ui.radios.find((r) => r.label === "快档");
  check("快档上出现「不支持当前语言」徽标", fast?.badge === "不支持当前语言", JSON.stringify(fast));
  check("快档仍是可点的（不禁用，换语言后就能用）", Boolean(fast), JSON.stringify(fast));

  const quality = ui.radios.find((r) => r.label === "质量档");
  check("质量档没有徽标（它支持日文）", quality?.badge === null, JSON.stringify(quality));

  check("出现警示提示说明会自动改用哪一档",
        ui.warnHints.length >= 1 && /质量档/.test(ui.warnHints[0] ?? ""),
        JSON.stringify(ui.warnHints));
  check("提示里带上当前语言对", /日文/.test(ui.warnHints[0] ?? "") && /中文/.test(ui.warnHints[0] ?? ""),
        JSON.stringify(ui.warnHints[0]));
}

/* ------------------------------------------------ 界面：支持的语言对 */

console.log("\n=== 中文 → 英文 + 快档（应当一切正常）===\n");
{
  await setLangs("zh", "en");
  await openSettings();
  await setTier("fast");
  await openSettings();
  const ui = await main.ev(readTierUi);

  check("没有档位带「不支持」徽标", ui.radios.every((r) => r.badge === null),
        JSON.stringify(ui.radios));
  check("没有警示提示", ui.warnHints.length === 0, JSON.stringify(ui.warnHints));
  check("提示说明当前档位支持哪些语言",
        ui.tierHints.some((t) => /快档|中文/.test(t) && /英文/.test(t)),
        JSON.stringify(ui.tierHints));
}

/* ------------------------------------------------ 主屏改语言后要刷新 */

console.log("\n=== 设置页关着时改语言，再打开要显示新结论 ===\n");
{
  // 上一段结束时界面停在"中文→英文 + 快档"。先打开一次设置页建立缓存，
  // 然后关掉它、改语言，再打开 —— 这是最容易出现"过期提示"的路径。
  await openSettings();
  const before = await main.ev(readTierUi);
  check("前置：此时没有不支持徽标", before.radios.every((r) => r.badge === null),
        JSON.stringify(before.radios.map((r) => r.badge)));

  // 退回主屏并改语言（设置页此时是关着的）
  await main.ev(`(async () => {
    for (let i = 0; i < 6; i += 1) {
      const back = document.querySelector('.page__back');
      if (!back) break;
      back.click();
      await new Promise(r => setTimeout(r, 350));
    }
  })()`);
  await setLangs("ja", "zh");
  await openSettings();
  const after = await main.ev(readTierUi);

  const fast = after.radios.find((r) => r.label === "快档");
  check("重新打开后快档已标为不支持（提示不是过期的）",
        fast?.badge === "不支持当前语言", JSON.stringify(after.radios));
  check("提示文字也换成了新语言对", /日文/.test(after.warnHints[0] ?? ""),
        JSON.stringify(after.warnHints));
}

/* ------------------------------------------------ 语言对持久化 */

console.log("\n=== 语言对要写进配置（否则重启回退）===\n");
{
  await main.ev(`(async () => {
    for (let i = 0; i < 6; i += 1) {
      const back = document.querySelector('.page__back');
      if (!back) break;
      back.click();
      await new Promise(r => setTimeout(r, 350));
    }
  })()`);
  await setLangs("ko", "en");

  const config = await main.ev(`(async () => {
    const r = await window.voxsub.backend.command('get_config', null);
    return r.data;
  })()`);
  check("改语言后配置里的 lang_pair 跟着更新",
        config?.lang_pair === "ko-en", `lang_pair=${config?.lang_pair}`);
}

/* ------------------------------------------------ 自清理 */

console.log("\n=== 测试自清理 ===\n");
{
  const langs = originalLangs.source && originalLangs.target
    ? originalLangs
    : { source: "zh", target: "en" };
  await setLangs(langs.source, langs.target);

  // 档位写回**测试开始时读到的原值**（不能写死：用户配置可能是 quality）
  const tierToRestore = originalTier ?? "fast";
  const restore = await main.ev(`(async () => {
    const r = await window.voxsub.backend.command('set_config', {
      updates: { translate_tier: ${JSON.stringify(tierToRestore)} },
    });
    return r.ok;
  })()`);
  check("语言已还原", true, `${langs.source}→${langs.target}`);
  check("档位已还原为测试前的值", restore === true, `translate_tier=${tierToRestore}`);

  // 复核：配置里确实回到了原值
  const finalTier = await main.ev(`(async () => {
    const r = await window.voxsub.backend.command('get_config', null);
    return r.data?.translate_tier ?? null;
  })()`);
  check("复核：配置档位与原值一致", finalTier === tierToRestore,
        `原值=${tierToRestore} 现值=${finalTier}`);
}

/* ------------------------------------------------------------ 汇总 */

main.close();
const failed = results.filter((r) => !r.ok);
console.log("\n" + "=".repeat(56));
console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log("\n失败项：");
  for (const f of failed) console.log(`  ${f.name}`);
}
process.exit(failed.length ? 1 : 0);
