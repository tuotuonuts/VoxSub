import assert from "node:assert/strict";
import { installMiniDom } from "./mini-dom.mjs";
import { importShared } from "./esbuild-ts.mjs";
process.env.TZ = "Asia/Kuala_Lumpur";
const dom = installMiniDom();
const mod = await importShared("tools/test-log-time-entry.ts", { bundle: true });
const { store } = mod;
// Explicit privacy-export consent; cancellation is covered by diagnostics-observability.
dom.window.confirm = () => true;
store.applyEvent({ type: "job", jobId: "fixture", command: "run_self_check", status: "running", sequence: 1, ts: "2026-09-26T17:25:20.775Z" });
const job = store.get().logs.at(-1);
assert.equal(job.ts, "2026-09-26T17:25:20.775Z", "job log retains producer event time, not renderer now");
assert.equal(job.eventTimeMs, Date.parse(job.ts));
assert.equal(job.source, "job");
assert.ok(Number.isFinite(job.receivedAtMs));
console.log("PASS job event absolute time and receive time");
const page = mod.buildDiagnostics();
const buttons = [...page.element.querySelectorAll("button")];
buttons.find(b => b.textContent === "实时日志").click();
assert.equal(page.element.querySelector(".log-row__ts").textContent, "2026-09-27 01:25:20.775 UTC+08:00");
console.log("PASS real diagnostics renders local date + milliseconds + offset");
page.dispose();

// Unknown/legacy timestamps must not be guessed from this machine's timezone/date.
const time = await importShared("src/shared/log-time.ts", { bundle: true });
const normalized = ts => time.normalizeLog({ ts, level: "INFO", message: "fixture" }, 100, 1);
for (const ts of ["", "bad", "01:25:22", "2026-09-27T01:25:22", "2026-09-27 01:25:22", "2026-02-30T10:00:00Z", "2026-09-27T24:00:00Z", "2026-09-27T01:00:00+99:00"]) {
  const e = normalized(ts);
  assert.equal(e.eventTimeMs, null, `reject incomplete/invalid ${ts}`);
  assert.match(time.formatLogTime(e), /无法确定绝对时刻/);
  assert.equal(e.ts, ts);
}
assert.equal(time.formatLogTime(normalized("2026-09-26T17:25:20.775Z")), time.formatLogTime(normalized("2026-09-27T01:25:20.775+08:00")));
for (const [zone, cases] of Object.entries({
  "Asia/Kuala_Lumpur": [["2026-09-26T15:59:59.999Z", "2026-09-26 23:59:59.999 UTC+08:00"], ["2026-09-26T16:00:00.001Z", "2026-09-27 00:00:00.001 UTC+08:00"]],
  "UTC": [["2026-09-26T17:25:20.007Z", "2026-09-26 17:25:20.007 UTC+00:00"]],
  "America/New_York": [["2026-03-08T06:59:59.999Z", "2026-03-08 01:59:59.999 UTC-05:00"], ["2026-03-08T07:00:00.000Z", "2026-03-08 03:00:00.000 UTC-04:00"], ["2026-11-01T05:30:00.000Z", "2026-11-01 01:30:00.000 UTC-04:00"], ["2026-11-01T06:30:00.000Z", "2026-11-01 01:30:00.000 UTC-05:00"]],
  "Asia/Kathmandu": [["2026-09-26T18:15:00.001Z", "2026-09-27 00:00:00.001 UTC+05:45"]],
})) {
  process.env.TZ = zone;
  for (const [ts, expected] of cases) assert.equal(time.formatLogTime(normalized(ts)), expected, zone);
}
process.env.TZ = "Asia/Kuala_Lumpur";
store.patch({ logs: [] });
for (const [message, ts] of [["first", "2026-09-27T01:00:00+08:00"], ["late", "2026-09-26T16:59:59Z"], ["tie", "2026-09-27T01:00:00+08:00"]]) {
  store.applyEvent({ type: "log", message, ts, level: "INFO" });
}
store.pushLog({ ts: "2026-09-26T17:00:00Z", message: "renderer", level: "INFO" });
assert.deepEqual(store.get().logs.map(e => e.message), ["first", "late", "tie", "renderer"]);
const sequences = store.get().logs.map(e => e.receiveSequence);
assert.deepEqual(sequences, [...sequences].sort((a,b) => a-b));
assert.equal(new Set(sequences).size, 4);
assert.deepEqual(store.get().logs.map(e => e.source), ["backend", "backend", "backend", "renderer"]);
console.log("PASS invalid/legacy, UTC+offset equivalence, midnight, milliseconds, four explicit TZ including DST, late/tied receive stream");

let exported;
let fileText = "2026-09-26T17:25:20.775+00:00 INFO     [fixture] original  \n2026-09-27 01:25:22 WARNING  [fixture] old\n01:25:23 INFO [fixture] older";
dom.window.voxsub = {
  dialog: { saveReport: async () => "isolated-test.txt" },
  backend: { command: async (cmd, args) => {
    if (cmd === "recent_logs") return { ok: true, data: { text: fileText, lines: 3 } };
    if (cmd === "export_diagnostics") { exported = args.log_text; return { ok: true, data: { path: args.path } }; }
    return { ok: true, data: { results: [] } };
  } },
};
mod.markBackendReady();
const view = mod.buildDiagnostics();
const button = text => [...view.element.querySelectorAll("button")].find(b => b.textContent === text);
button("实时日志").click();
button("导出日志").click();
await dom.flushAsync(8);
for (const row of view.element.querySelectorAll(".log-row")) assert.ok(exported.includes(row.querySelector(".log-row__ts").textContent));
button("历史文件").click();
await dom.flushAsync(8);
const rows = [...view.element.querySelectorAll(".log-row")];
assert.equal(rows[0].querySelector(".log-row__ts").textContent, "2026-09-27 01:25:20.775 UTC+08:00");
assert.match(rows[1].querySelector(".log-row__ts").textContent, /无法确定绝对时刻/);
assert.match(rows[2].querySelector(".log-row__ts").textContent, /无法确定绝对时刻/);
button("导出日志").click();
await dom.flushAsync(8);
for (const row of rows) assert.ok(exported.includes(row.querySelector(".log-row__ts").textContent));
assert.ok(exported.includes(JSON.stringify(fileText.split("\n")[0])), "export retains exact raw record including trailing spaces");
view.dispose();
console.log("PASS production live/file diagnostics display and export semantics, raw evidence preserved");
const clocks = await importShared("src/shared/log-levels.ts");
assert.match(clocks.localIsoNow(), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|[+-]\d{2}:\d{2})$/);
console.log("PASS Electron local log producer emits absolute timestamp");
