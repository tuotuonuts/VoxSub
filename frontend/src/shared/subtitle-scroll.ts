/**
 * 字幕流的滚动跟随策略。
 *
 * 规则（按用户实际滚动位置判断，不用定时器猜）：
 *   · 更新前位于底部附近 → 更新后继续贴底；
 *   · 用户上滚看历史 → 新内容不把视口拉回底部；
 *   · 用户自己滚回底部 → 恢复跟随。
 *
 * 必须在 DOM 变更**之前**测量：变更后 scrollHeight 已经变大，"是否在底部"就失真了。
 */
export const FOLLOW_THRESHOLD_PX = 48;

export interface ScrollBox {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** 内容不足以滚动时视为在底部（此时跟随是无害的）。 */
export function isNearBottom(box: ScrollBox, threshold = FOLLOW_THRESHOLD_PX): boolean {
  return box.scrollHeight - box.clientHeight - box.scrollTop <= threshold;
}

/** 在 mutate 前测量、后决定是否贴底。返回是否跟随了。 */
export function updateFollowing(box: ScrollBox, mutate: () => void, threshold = FOLLOW_THRESHOLD_PX): boolean {
  const follow = isNearBottom(box, threshold);
  mutate();
  if (follow) box.scrollTop = box.scrollHeight;
  return follow;
}
