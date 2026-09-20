/**
 * 危险操作的确认闸门（工作单 §3.7 的 ConfirmAction）。
 *
 * ## 为什么这么薄还要抽
 *
 * 全应用只有两处破坏性操作（诊断页「清除本机日志」、迁移向导「清理已迁移的原目录」），
 * 但两处靠的是同一句手写契约：**没确认就绝不能动**。清理那一处更狠 ——
 * 后端要求显式 `confirm: true`，少了它会被拒；而命令一旦发出去，删掉的是
 * 用户的原目录，不可逆。
 *
 * 把"先问，再动"固定成一个函数，收益不是省行数，而是：
 *   · 取消路径与执行路径**只有一种写法**，新增破坏性操作时不会漏掉确认；
 *   · `ask` / `action` / `onDecline` 都是参数，因此"取消时 action 绝不执行、
 *     onDecline 一定执行"可以被离线测试钉住（`tools/test-confirm-action.mjs`）；
 *   · 确认弹窗**必须同步**——`ask` 返回的是用户当下的决定，异步确认（先渲染
 *     自定义弹窗再问）会让"确认"与"发命令"脱钩，这里刻意不接受 Promise。
 *
 * 注意：这里不碰 DOM、不碰 i18n —— 确认文案由调用方经 `tr()` 传入，
 * 组件只负责"问"，以及"据此决定走哪条路"。
 */

/**
 * 先确认，再执行；未确认则走取消分支。
 *
 * @param ask 弹确认并返回用户是否同意（生产代码传 `() => window.confirm(tr("…"))`）
 * @param action 只有确认之后才会执行的动作
 * @param onDecline 用户取消时执行的收尾（例如在界面上说明"已取消"）；
 *   省略则什么都不做
 * @returns 是否执行了 `action`（false = 用户取消，`action` **一次都没被调用**）
 */
export function runAfterConfirm(ask: () => boolean, action: () => void, onDecline?: () => void): boolean {
  if (!ask()) {
    onDecline?.();
    return false;
  }
  action();
  return true;
}
