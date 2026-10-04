import assert from "node:assert/strict";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared } from "./esbuild-ts.mjs";

const dom = installMiniDom();
let page, disconnect;
let checks = 0;
const test = async (name, run) => { await run(); checks++; console.log("PASS " + name); };
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); };
try {
  const api = await importShared("tools/test-command-feedback-entry.ts", { bundle: true });
  const { CMD, describeJobFeedback, store, connectBackend, setUiTranslator,
          tr, setLanguage, buildWorkspace, updateStatus } = api;
  setUiTranslator(tr);
  const events = new Set();
  dom.window.voxsub = { backend: {
    start: async () => ({ ok: true }),
    onEvent: fn => { events.add(fn); return () => events.delete(fn); },
    command: async command => ({ ok: true, data: command === CMD.state
      ? { mode: "a", running: false, paused: false }
      : command === CMD.getConfig ? { recording_enabled: false } : {} }),
  } };
  disconnect = connectBackend();
  for (const fn of events) fn({ type: "ready", version: "test", session: { mode: "a", running: false } });
  await settle();
  page = buildWorkspace();
  dom.mount(page.element);
  const visible = () => page.element.querySelector(".status-lamp__text").textContent;
  const job = (command, status, extra = {}) => {
    store.applyEvent({ type: "job", jobId: "private-job-id", command, status, ...extra });
    updateStatus();
  };

  await test("production home shows settings saved, not set_config", () => {
    setLanguage("zh");
    job(CMD.setConfig, "succeeded");
    assert.equal(visible(), "设置已保存");
    assert.doesNotMatch(visible(), /set_config|private-job-id|succeeded/);
    assert.ok(store.get().logs.some(log => log.message.includes("set_config")));
  });
  await test("language/model/recording selection uses understandable copy", () => {
    for (const [command, expected] of [
      [CMD.setLangs, "翻译语言已更新"],
      [CMD.setAsrModel, "识别模型选择已更新"],
      [CMD.setTranslator, "翻译模型选择已更新"],
      [CMD.setRecording, "录音设置已更新"],
    ]) {
      job(command, "succeeded");
      assert.equal(visible(), expected);
      assert.equal(store.get().running, false);
    }
  });
  await test("failure and cancellation remain distinct without internal names", () => {
    job(CMD.setConfig, "failed", { error: "internal failure details" });
    assert.match(visible(), /保存设置.*失败/);
    assert.doesNotMatch(visible(), /set_config|private-job-id|internal failure/);
    job(CMD.setConfig, "cancelled");
    assert.match(visible(), /保存设置.*已取消/);
    assert.doesNotMatch(visible(), /失败|已完成|set_config/);
  });
  await test("queued/running/cancelling never announce completion", () => {
    store.patch({ statusText: "正在处理设置" }); updateStatus();
    for (const status of ["queued", "running", "cancelling"]) {
      job(CMD.setConfig, status);
      assert.equal(visible(), "正在处理设置");
      assert.equal(store.get().running, false);
    }
    job(CMD.setConfig, "cancelled");
    assert.match(visible(), /已取消/);
  });
  await test("unknown/missing command hides IDs and prototype property names", () => {
    for (const command of ["future_internal_command", "__proto__", "constructor", "toString", undefined]) {
      job(command, "succeeded");
      assert.equal(visible(), "后台任务：已完成");
      assert.doesNotMatch(visible(), /private-job-id|future_|__proto__|constructor|toString/);
    }
  });
  await test("every known IPC command has a readable terminal label", () => {
    for (const command of Object.values(CMD)) {
      for (const status of ["succeeded", "failed", "cancelled"]) {
        const message = describeJobFeedback(command, status);
        assert.ok(message.length > 0);
        assert.ok(!message.includes(command), message);
        assert.ok(!message.startsWith("后台任务"), command);
      }
    }
    assert.equal(describeJobFeedback("future_internal", "unexpected"), "任务状态尚未确认");
  });
  await test("English home status uses translated copy", () => {
    setLanguage("en");
    job(CMD.setConfig, "succeeded");
    assert.equal(visible(), "Settings saved");
    for (const command of Object.values(CMD)) {
      for (const status of ["succeeded", "failed", "cancelled"]) {
        const message = describeJobFeedback(command, status, tr);
        assert.doesNotMatch(message, /[\u3400-\u9fff]/, message);
        assert.ok(!message.includes(command), message);
      }
    }
    job(CMD.setConfig, "failed");
    assert.match(visible(), /Save settings.*Failed/);
    job(CMD.setConfig, "cancelled");
    assert.match(visible(), /Save settings.*cancelled/i);
    setLanguage("zh");
  });
  await test("completion of an action does not claim runtime/model health", () => {
    assert.equal(describeJobFeedback(CMD.start, "succeeded"), "启动请求已处理");
    assert.equal(describeJobFeedback(CMD.cancelJob, "succeeded"), "取消请求已提交");
    assert.doesNotMatch(describeJobFeedback(CMD.setAsrModel, "succeeded"), /已加载|正在运行/);
    assert.doesNotMatch(describeJobFeedback(CMD.runSelfCheck, "succeeded"), /全部.*正常|全部.*通过/);
  });
  await test("backend operational statuses still reach the home unchanged", () => {
    store.applyEvent({ type: "status", text: "正在识别语音…" }); updateStatus();
    assert.equal(visible(), "正在识别语音…");
  });
  console.log(`PASS user-facing command feedback ${checks}/${checks}`);
} finally {
  page?.dispose();
  disconnect?.();
  dom.restore();
}
