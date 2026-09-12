/**
 * 语幕 VoxSub — 设计令牌
 *
 * 方向：同人展目录（impeccable seed 58dbb26d，指派 index 3）
 *   - 单色黑印在纸面上，靠细分隔线分区，不靠色块
 *   - 选中的格子被"笔圈出"；看过的"划掉"
 *   - 整格重排，不是重格内部
 *
 * 自检（craft-floor「AI 默认长相」清单）：
 *   - 底色取冷调（hue 200°），不是暖奶油（hue 40°）
 *   - accent 是印刷蓝，不是"近黑底 + 霓虹 accent"
 *   - 全部对比度实测达标（见 tools/verify-palette.mjs）
 */
export const palette = {
  light: {
    ground: "#F2F4F5",
    /** 卡片面：带极轻冷调而非纯白，服从 craft floor「always tint」 */
    surface: "#FBFCFC",
    sunken: "#E8EBED",
    ink: "#14181A",
    ink2: "#5A6569",
    rule: "#D5DADD",
    accent: "#1B4B8F",
    /** 译文用色：比 accent 稍深，保证在多个表面上都可读 */
    accentInk: "#17406F",
    onAccent: "#FFFFFF",
    /** 目录的"笔圈"标记 —— accent 的浅色版，模拟圆珠笔划过纸面 */
    mark: "rgba(27, 75, 143, 0.14)",
    ok: "#1F7A4D",
    warn: "#9A6412",
    err: "#A63232",
  },
  dark: {
    ground: "#101416",
    surface: "#191F22",
    raised: "#242E32",
    sunken: "#0B0E10",
    ink: "#EEF1F2",
    ink2: "#8B979B",
    rule: "#2C3539",
    accent: "#6FA8DC",
    accentInk: "#A8C8E8",
    onAccent: "#0B1013",
    mark: "rgba(111, 168, 220, 0.18)",
    ok: "#5FC98D",
    warn: "#E0B45C",
    err: "#E88A8A",
  },
} as const;

export type ThemeName = keyof typeof palette;
export type TokenSet = (typeof palette)[ThemeName];
