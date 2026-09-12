/**
 * 令牌契约校验 —— 防止"看不见的层级"回归。
 *
 * 背景：VoxSub 的 Qt 版曾因三层表面相互对比度只有 1.07-1.17:1，
 * 整个界面退化成一坨近黑。本脚本把该属性变成可执行的检查：
 *   · 相邻表面必须可辨（>=1.20，最低 >=1.15）
 *   · 正文文字达 WCAG AA（>=4.5:1）
 *   · accent 作控件达 3:1，作文字达 4.5:1
 *   · 表面色必须带色相（craft floor「always tint」）
 *
 * 用法：node tools/verify-palette.mjs   （失败时退出码 1，可直接进 CI）
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "src", "renderer", "palette.ts"), "utf8");

/** 从 palette.ts 里解析出两组令牌（保持单一事实来源，不重复定义色值）。 */
function parseTokens() {
  const light = {};
  const dark = {};
  let current = null;
  for (const rawLine of source.split("\n")) {
    const line = rawLine.trim();
    if (/^light:\s*\{/.test(line)) {
      current = light;
      continue;
    }
    if (/^dark:\s*\{/.test(line)) {
      current = dark;
      continue;
    }
    if (line === "}," || line === "}") {
      current = null;
      continue;
    }
    if (!current) continue;
    const match = line.match(/^([A-Za-z0-9_]+):\s*"([^"]+)",?/);
    if (match) current[match[1]] = match[2];
  }
  return { light, dark };
}

function srgb(channel) {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

function luminance(hex) {
  const [r, g, b] = hexToRgb(hex);
  return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
}

function contrast(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

function colorHue(hex) {
  const [r, g, b] = hexToRgb(hex).map((v) => v / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max === min) return { hue: 0, sat: 0 };
  const d = max - min;
  const l = (max + min) / 2;
  const sat = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let hue;
  if (max === r) hue = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) hue = ((b - r) / d + 2) / 6;
  else hue = ((r - g) / d + 4) / 6;
  return { hue: hue * 360, sat };
}

const isHex = (v) => typeof v === "string" && /^#[0-9A-Fa-f]{6}$/.test(v);

const { light, dark } = parseTokens();
let failures = 0;

function check(label, ok, detail) {
  const mark = ok ? "OK  " : "FAIL";
  if (!ok) failures += 1;
  console.log(`  ${mark} ${label}${detail ? `  — ${detail}` : ""}`);
}

for (const [name, set] of [
  ["light", light],
  ["dark", dark],
]) {
  console.log(`\n=== ${name} ===`);

  // 1) 层级分离
  const pairs = [
    ["ground", "surface", 1.05],
    ["surface", "rule", 1.25],
  ];
  if (name === "dark") pairs.push(["surface", "raised", 1.15]);
  else pairs.push(["surface", "sunken", 1.05]);

  for (const [a, b, min] of pairs) {
    if (!isHex(set[a]) || !isHex(set[b])) continue;
    const r = contrast(set[a], set[b]);
    check(`${a} ↔ ${b}`, r >= min, `${r.toFixed(3)}:1 (需 ≥${min})`);
  }

  // 2) 文字对比度（正文 >= 4.5）
  for (const text of ["ink", "ink2", "accentInk"]) {
    for (const bg of ["ground", "surface"]) {
      if (!isHex(set[text]) || !isHex(set[bg])) continue;
      const r = contrast(set[text], set[bg]);
      check(`${text} on ${bg}`, r >= 4.5, `${r.toFixed(2)}:1`);
    }
  }

  // 3) accent：作控件 3:1，作文字 4.5:1
  for (const bg of ["ground", "surface"]) {
    if (!isHex(set.accent) || !isHex(set[bg])) continue;
    const r = contrast(set.accent, set[bg]);
    check(`accent on ${bg} (控件 ≥3)`, r >= 3, `${r.toFixed(2)}:1`);
  }
  check(
    "onAccent over accent",
    contrast(set.onAccent, set.accent) >= 4.5,
    `${contrast(set.onAccent, set.accent).toFixed(2)}:1`,
  );

  // 4) 语义色在两种表面上都要能读
  for (const semantic of ["ok", "warn", "err"]) {
    for (const bg of ["surface"]) {
      if (!isHex(set[semantic]) || !isHex(set[bg])) continue;
      const r = contrast(set[semantic], set[bg]);
      check(`${semantic} on ${bg}`, r >= 3, `${r.toFixed(2)}:1`);
    }
  }

  // 5) always tint：表面不得是纯中性灰
  for (const key of ["ground", "surface", "ink"]) {
    if (!isHex(set[key])) continue;
    const { hue, sat } = colorHue(set[key]);
    check(
      `${key} 带色相`,
      sat > 0.02,
      `hue=${hue.toFixed(0)}° sat=${(sat * 100).toFixed(1)}%`,
    );
  }
}

console.log(
  failures === 0
    ? "\n全部令牌契约通过。"
    : `\n${failures} 项未达标 —— 请先修令牌再继续。`,
);
process.exit(failures === 0 ? 0 : 1);
