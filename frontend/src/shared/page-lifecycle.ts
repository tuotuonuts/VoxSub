/**
 * 页面生命周期 —— 纯逻辑（不依赖 Electron，可在 node 里单测）。
 *
 * ## 缺陷背景（工作单 #10：页面切换只增不减的监听器）
 *
 * 渲染层原先没有任何 dispose / removeEventListener / AbortController：
 * 页面切换只用 `pageLayer.replaceChildren(...)`，**注册在 `window`/`document`
 * 上的监听器却留着**。每打开一次设置页就多一个 `voxsub:settings` 订阅，
 * 每个订阅都还闭包持有一份已被移除的 DOM 与页面局部状态；工作区每切一次模式
 * 就多一个 `voxsub:state` 监听，外加一个永远不会被清掉的 1 秒定时器
 * （`workspace.ts` 的 `startClock()`）。
 *
 * 修法：页面构建返回 `{ element, dispose }`，把"这一页注册过什么"记在
 * `PageLifecycle` 上，页面被关闭/替换时统一释放。
 *
 * ## 纪律
 *
 * · **幂等**：`dispose()` 可以被调用任意多次（换页 + 关页 + 退出都可能触发），
 *   释放动作只执行一次；
 * · **释放后不再接受注册**：晚到的 `add()` 立刻执行并丢弃，避免"页面已死、
 *   订阅仍活着"；
 * · **失效保护**：`guard()` 包住的异步回调在页面失效后直接返回，
 *   不再往已移除的节点上写（诊断页自检要跑 4-6 秒，正是这种场景）；
 * · 单个 disposer 抛错不影响其余释放（否则一个坏回调会让后面全泄漏）。
 */

export type Disposer = () => void;

/** 只需要"能加/能减监听"的最小接口 —— 便于单测传假对象。 */
export interface EventTargetLike {
  addEventListener(
    type: string,
    listener: (event: Event) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
  removeEventListener(
    type: string,
    listener: (event: Event) => void,
    options?: boolean | AddEventListenerOptions,
  ): void;
}

/**
 * 页面句柄：页面构建的返回值。
 *
 * `element` 替换进容器；`dispose` 在页面被关闭/替换时调用一次。
 */
export interface PageHandle {
  element: HTMLElement;
  dispose: Disposer;
}

/** 无状态页面的句柄（没有监听器/定时器要释放）。 */
export function staticPage<T extends HTMLElement>(element: T): { element: T; dispose: Disposer } {
  return { element, dispose: () => undefined };
}

export class PageLifecycle {
  private disposers: Disposer[] = [];
  private closed = false;

  /** 页面是否已经失效。异步回调靠它决定要不要继续写 DOM。 */
  get disposed(): boolean {
    return this.closed;
  }

  /**
   * 登记一个释放动作。
   *
   * 已失效时**立即执行**并返回：晚到的注册不该把资源留在外面。
   */
  add(disposer: Disposer): void {
    if (this.closed) {
      runQuietly(disposer);
      return;
    }
    this.disposers.push(disposer);
  }

  /**
   * 绑定 DOM 事件并在 dispose 时移除（同一份 handler 引用，否则移除无效）。
   *
   * 页面已失效时**不注册** —— 注册了也不会有人来移除，正好造出这个类要消灭的
   * 那种悬挂订阅。
   */
  listen(
    target: EventTargetLike,
    type: string,
    handler: (event: Event) => void,
    options?: boolean | AddEventListenerOptions,
  ): void {
    if (this.closed) return;
    target.addEventListener(type, handler, options);
    this.add(() => target.removeEventListener(type, handler, options));
  }

  /** 登记一个定时器，dispose 时清掉。返回停止函数以便提前停止。 */
  interval(handler: () => void, ms: number): Disposer {
    if (this.closed) return () => undefined;
    const handle = setInterval(handler, ms);
    const stop = (): void => clearInterval(handle);
    this.add(stop);
    return stop;
  }

  /**
   * 建一个 AbortController，dispose 时 abort。
   *
   * 用于 fetch / 需要一次性取消整组异步操作的场景：AbortController 只提供
   * "取消"信号，页面失效时 abort 不会把结果误判成成功。
   */
  controller(): AbortController {
    const controller = new AbortController();
    if (this.closed) {
      controller.abort();
      return controller;
    }
    this.add(() => controller.abort());
    return controller;
  }

  /**
   * 包一层回调：页面失效后调用它什么都不做。
   *
   * 典型用法是 `void load().then(lifecycle.guard(() => renderPane()))`。
   */
  guard<A extends unknown[]>(fn: (...args: A) => void): (...args: A) => void {
    return (...args: A): void => {
      if (this.closed) return;
      fn(...args);
    };
  }

  /** 释放全部资源。幂等：第二次及以后调用不做事，也不会重复执行 disposer。 */
  dispose(): void {
    if (this.closed) return;
    this.closed = true;
    // 后进先出：后建的资源依赖先建的（例如监听器用到先建的定时器）
    const pending = this.disposers.splice(0, this.disposers.length).reverse();
    for (const disposer of pending) runQuietly(disposer);
  }
}

/** 单个 disposer 抛错不能让其余释放被跳过 —— 否则一个坏回调会造出整片泄漏。 */
function runQuietly(disposer: Disposer): void {
  try {
    disposer();
  } catch (error) {
    // 释放路径不抛错：页面已经在拆了，再抛只会打断拆除
    console.error("[lifecycle] 释放失败", error);
  }
}
