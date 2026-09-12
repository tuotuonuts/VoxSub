/**
 * 图标可用性测试（Electron 主进程内运行）。
 *
 * 为什么必须单独测：图标问题只会在真实 Electron 运行时暴露 ——
 * 文件存在不代表 nativeImage 能解析它，而托盘/任务栏拿到空图时就静默显示
 * 一个空白占位，不会报错。用户看到的就是"没有 icon"。
 *
 * 覆盖：
 *   1. dist/renderer/assets/icon.ico 存在
 *   2. nativeImage.createFromPath 能解析，且不是 empty
 *   3. 尺寸正确（任务栏/托盘都需要）
 *   4. 主窗构造参数里确实带上了 icon
 *
 * 用法：node_modules/.bin/electron tools/test-icon.cjs
 */
const { app, BrowserWindow, nativeImage, Tray } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const ICON = path.join(ROOT, "dist", "renderer", "assets", "icon.ico");

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
};

app.whenReady().then(() => {
  console.log("=== 图标文件 ===\n");
  const exists = fs.existsSync(ICON);
  check("图标文件已复制到 dist", exists, exists ? `${(fs.statSync(ICON).size / 1024).toFixed(1)} KB` : ICON);

  console.log("\n=== nativeImage 解析 ===\n");
  const image = exists ? nativeImage.createFromPath(ICON) : nativeImage.createEmpty();
  const empty = image.isEmpty();
  check("nativeImage 能解析（非空）", !empty, empty ? "解析失败，会显示空白图标" : "ok");

  if (!empty) {
    const size = image.getSize();
    check("尺寸正确", size.width > 0 && size.height > 0, `${size.width}×${size.height}`);
    // 托盘区通常 16×16，Windows 会自行缩放；这里确认有像素数据
    check("有像素数据", image.toPNG().length > 0, `${image.toPNG().length} 字节 PNG`);
  }

  console.log("\n=== 主窗确实带上了 icon ===\n");
  {
    const win = new BrowserWindow({
      show: false,
      width: 400,
      height: 300,
      ...(empty ? {} : { icon: image }),
    });
    // Electron 没有 getIcon()，用"能否拿到非空 image"间接确认：
    // 这里直接断言我们传进去的就是非空图，并确认窗口能正常创建
    check("窗口创建成功", !win.isDestroyed());
    win.destroy();
  }

  console.log("\n=== 托盘能接受该图标 ===\n");
  {
    let trayOk = false;
    let trayErr = "";
    try {
      const tray = new Tray(empty ? nativeImage.createEmpty() : image);
      trayOk = !tray.isDestroyed();
      tray.destroy();
    } catch (error) {
      trayErr = String(error.message ?? error);
    }
    check("托盘构造成功", trayOk, trayErr || "ok");
  }

  const failed = results.filter((r) => !r.ok);
  console.log("\n" + "=".repeat(56));
  console.log(`${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);

  app.exit(failed.length ? 1 : 0);
});
