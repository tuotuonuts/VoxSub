/**
 * electron-builder 打包配置。
 *
 * 产物布局（安装后）：
 *   <安装目录>/
 *     VoxSub.exe                  Electron 主程序
 *     resources/
 *       app.asar/                 前端代码（main + renderer）
 *       backend/                  Python sidecar（PyInstaller onedir）
 *         VoxSubBackend.exe
 *         _internal/              运行时依赖
 *
 * 关键决策：
 *
 * 1. **sidecar 不带 Qt。** PyInstaller spec 里 excludes 掉 PySide6/shiboken6/
 *    qfluentwidgets —— 核心层在 Qt 移除后零 Qt 依赖，省约 115MB。
 *
 * 2. **模型不进安装包。** 首次运行由后端 `_ensure_first_run_defaults()` 决定
 *    模型目录（非系统盘优先），用户在模型广场按需下载。安装包因此保持在
 *    几百 MB 量级，而不是 25GB。
 *
 * 3. **asar 只包前端代码。** sidecar 是外部可执行文件，必须解压在 asar 之外，
 *    Electron 才能 spawn 它（asarPack 内的 exe 无法直接执行）。
 *
 * 4. **不签名。** 当前是自签阶段；正式 OV 证书到位后在 win.sign 加上。
 *    未签名会导致 SmartScreen 提示，需在发布说明里给出绕过指引。
 */
const path = require("node:path");

/** sidecar 产物目录（PyInstaller onedir 输出）。 */
const BACKEND_DIST = path.join(__dirname, "backend", "dist", "VoxSubBackend");

module.exports = {
  appId: "com.voxsub.electron",
  productName: "VoxSub",
  copyright: "VoxSub",

  // 版本号与 package.json 保持一致；发布时两处一起改
  directories: {
    output: path.join(__dirname, "..", "..", "Release", "electron"),
    buildResources: "build",
  },

  // 打包进 asar 的内容：只有前端代码与静态资源
  files: [
    "dist/main/**/*",
    "dist/renderer/**/*",
    "package.json",
    "!**/*.map",
    "!**/*.ts",
    "!node_modules/**/*",
  ],

  // sidecar 与文档放在 asar 外
  extraResources: [
    { from: BACKEND_DIST, to: "backend", filter: ["**/*"] },
    { from: "README.md", to: "docs/README.md" },
    { from: "STATUS.md", to: "docs/STATUS.md" },
  ],

  asar: true,

  win: {
    target: [{ target: "nsis", arch: ["x64"] }],
    // 图标：Qt 版有 assets/icon.ico，Electron 侧暂用同名文件；
    // 缺失时 electron-builder 会用默认图标（不阻断打包）
    icon: path.join(__dirname, "..", "..", "assets", "icon.ico"),
    // 不签名：自签阶段。正式证书到位后加 sign 配置。
    signAndEditExecutable: true,
    requestedExecutionLevel: "asInvoker", // 不需要管理员：模型写在非系统盘
  },

  nsis: {
    oneClick: false,
    perMachine: false, // 当前用户安装，不需要 UAC
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: "语幕 VoxSub",
    // 与 Qt 版用不同的安装目录与 AppId，避免两个安装器互相覆盖
    artifactName: "VoxSub-Electron-Setup-${version}.exe",
    uninstallDisplayName: "语幕 VoxSub (Electron)",
  },

  // 文件关联（后续可加 .srt）
  fileAssociations: [],
};
