#!/usr/bin/env node
/**
 * 页面生命周期（src/shared/page-lifecycle.ts）—— 缺陷 #10 的离线守护。
 *
 * ## 守的是什么
 *
 * 渲染层原先没有任何 dispose：页面切换只 `replaceChildren`，注册在 window/document
 * 上的监听器留着不走。开一次设置页多一个 `voxsub:settings` 订阅，切一次模式多一个
 * `voxsub:state` 监听 + 一个永不清除的 1 秒计时器。结果不是"立刻崩"，而是
 * 用得越久越慢、回调打到已经移除的节点上。
 *
 * 这个测试用假事件目标（不依赖 DOM）断言：
 *   · dispose 会**成对**移除监听器（类型/handler/options 完全一致，否则移除无效）；
 *   · dispose **幂等**：多次调用只释放一次；
 *   · 失效后不再接受注册（晚到的 add 立即执行，不留悬挂订阅）；
 *   · 失效后的回调不再执行（guard）；
 *   · 定时器/AbortController 被真正释放；
 *   · 一个 disposer 抛错不影响其他释放（否则一坏就整片泄漏）。
 *
 * 用法：node tools/test-page-lifecycle.mjs
 */
import { importShared, createReporter } from "./esbuild-ts.mjs";

const { check, finish } = createReporter("页面生命周期（缺陷 #10）");
const { PageLifecycle, staticPage } = await importShared("src/shared/page-lifecycle.ts");

/** 假事件目标：记录每一次 add/remove，便于断言"成对且引用一致"。 */
function fakeTarget() {
  return {
    calls: [],
    addEventListener(type, listener, options) {
      this.calls.push({ op: "add", type, listener, options });
    },
    removeEventListener(type, listener, options) {
      this.calls.push({ op: "remove", type, listener, options });
    },
  };
}

const removes = (target) => target.calls.filter((c) => c.op === "remove");
const adds = (target) => target.calls.filter((c) => c.op === "add");

console.log("=== 监听器成对移除 ===\n");
{
  const target = fakeTarget();
  const lifecycle = new PageLifecycle();
  const handler = () => undefined;
  lifecycle.listen(target, "voxsub:settings", handler);
  lifecycle.listen(target, "voxsub:state", handler, { passive: true });

  check("注册了 2 个监听", adds(target).length === 2, String(adds(target).length));
  check("dispose 之前未移除", removes(target).length === 0);

  lifecycle.dispose();
  check("dispose 后全部移除", removes(target).length === 2, String(removes(target).length));
  check(
    "移除时用的是同一个 handler 引用（否则 DOM 不会真的移除）",
    removes(target).every((c) => c.listener === handler),
  );
  const state = removes(target).find((c) => c.type === "voxsub:state");
  check(
    "移除时带上原来的 options（capture 不一致会移除不掉）",
    Boolean(state) && state.options && state.options.passive === true,
  );
  check(
    "移除的类型与注册一一对应（顺序无关，释放是后进先出）",
    removes(target).map((c) => c.type).sort().join(",") === "voxsub:settings,voxsub:state",
    removes(target).map((c) => c.type).join(","),
  );
}

console.log("\n=== dispose 幂等 ===\n");
{
  const target = fakeTarget();
  const lifecycle = new PageLifecycle();
  let released = 0;
  lifecycle.listen(target, "x", () => undefined);
  lifecycle.add(() => { released += 1; });

  lifecycle.dispose();
  lifecycle.dispose();
  lifecycle.dispose();

  check("释放动作只执行一次", released === 1, `实际 ${released} 次`);
  check("移除调用只发生一次", removes(target).length === 1, String(removes(target).length));
  check("disposed 属性为真", lifecycle.disposed === true);
  check("重复 dispose 之后 disposed 仍为真", (lifecycle.dispose(), lifecycle.disposed === true));
}

console.log("\n=== 失效后不再接受注册 ===\n");
{
  const target = fakeTarget();
  const lifecycle = new PageLifecycle();
  lifecycle.dispose();

  let lateRan = 0;
  lifecycle.add(() => { lateRan += 1; });
  check("失效后 add 立刻执行（不留悬挂资源）", lateRan === 1, `实际 ${lateRan} 次`);

  lifecycle.listen(target, "late", () => undefined);
  check("失效后 listen 不注册、也不残留", adds(target).length === 0 && removes(target).length === 0);

  // 失效后不该再建定时器（建了也只能立刻清掉，等于白占一个 id）
  const realSet = globalThis.setInterval;
  let created = 0;
  globalThis.setInterval = (...args) => { created += 1; return realSet(...args); };
  const stop = lifecycle.interval(() => undefined, 50);
  globalThis.setInterval = realSet;
  check("失效后 interval 不建定时器", created === 0, `实际建了 ${created} 个`);
  check("失效后 interval 返回的停止函数可安全调用", (() => { stop(); return true; })());
}

console.log("\n=== 异步回调的失效保护 ===\n");
{
  const lifecycle = new PageLifecycle();
  let writes = 0;
  const write = lifecycle.guard(() => { writes += 1; });
  write();
  check("失效前回调正常执行", writes === 1);
  lifecycle.dispose();
  write();
  write();
  check("失效后回调不执行（不再往已移除的节点写）", writes === 1, `实际 ${writes} 次`);
  check("guard 透传参数", (() => {
    let got = null;
    const l2 = new PageLifecycle();
    const fn = l2.guard((a, b) => { got = [a, b]; });
    fn(1, 2);
    return got && got[0] === 1 && got[1] === 2;
  })());
}

console.log("\n=== 定时器与 AbortController ===\n");
{
  // 用可控的假计时器，避免依赖真实时间
  const realSet = globalThis.setInterval;
  const realClear = globalThis.clearInterval;
  const timers = new Map();
  let nextId = 1;
  globalThis.setInterval = (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; };
  globalThis.clearInterval = (id) => { timers.delete(id); };

  const lifecycle = new PageLifecycle();
  let ticks = 0;
  const stop = lifecycle.interval(() => { ticks += 1; }, 1000);
  check("定时器已建立", timers.size === 1 && timers.get(1).ms === 1000);
  check("interval 返回停止函数", typeof stop === "function");

  const controller = lifecycle.controller();
  check("AbortController 初始未取消", controller.signal.aborted === false);

  lifecycle.dispose();
  check("dispose 清掉定时器", timers.size === 0, `剩余 ${timers.size}`);
  check("dispose 取消 AbortController", controller.signal.aborted === true);

  const disposed = new PageLifecycle();
  disposed.dispose();
  check("失效后新建的 controller 直接处于已取消态", disposed.controller().signal.aborted === true);

  globalThis.setInterval = realSet;
  globalThis.clearInterval = realClear;
}

console.log("\n=== 单个释放失败不影响其他 ===\n");
{
  const errors = [];
  const realError = console.error;
  console.error = (...args) => errors.push(args);

  const target = fakeTarget();
  const lifecycle = new PageLifecycle();
  let laterRan = false;
  lifecycle.add(() => { throw new Error("坏的释放动作"); });
  lifecycle.add(() => { laterRan = true; });
  lifecycle.listen(target, "after", () => undefined);

  lifecycle.dispose();

  console.error = realError;
  check("坏的 disposer 不阻断后面的释放", laterRan === true);
  check("监听器仍被移除", removes(target).length === 1);
  check("错误被记录而不是抛出", errors.length === 1, `记录 ${errors.length} 条`);
}

console.log("\n=== 场景对照：反复切页 ===\n");
{
  // 旧做法（只 replaceChildren）：每次切页都多留一个 window 监听
  const target = fakeTarget();
  const leaked = [];
  for (let i = 0; i < 3; i += 1) {
    const handler = () => undefined;
    target.addEventListener("voxsub:settings", handler);
    leaked.push(handler); // 没有任何地方移除它
  }
  const legacyRemaining = adds(target).length - removes(target).length;
  check("旧做法：3 次切页留下 3 个监听", legacyRemaining === 3, `实际 ${legacyRemaining}`);

  // 新做法：每页一个 lifecycle，换页时 dispose
  const target2 = fakeTarget();
  for (let i = 0; i < 3; i += 1) {
    const lifecycle = new PageLifecycle();
    lifecycle.listen(target2, "voxsub:settings", () => undefined);
    lifecycle.dispose();
  }
  const remaining = adds(target2).length - removes(target2).length;
  check("新做法：3 次切页后剩 0 个监听", remaining === 0, `实际 ${remaining}`);
}

console.log("\n=== 无状态页面 ===\n");
{
  const element = { tagName: "DIV" };
  const handle = staticPage(element);
  check("staticPage 保留元素引用", handle.element === element);
  check("staticPage 的 dispose 是安全空操作", (() => {
    handle.dispose();
    handle.dispose();
    return true;
  })());
  check("handle 形状是 { element, dispose }", typeof handle.dispose === "function" && "element" in handle);
}

finish();
