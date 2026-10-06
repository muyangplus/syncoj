/** 审计日志。 */

import { query, request } from './client'
import { listPage } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { EventOut, SimpleAck } from './types'

export const eventApi = {
  /**
   * 全部审计事件，不按场次过滤。
   *
   * 有些事件**根本不属于任何场次**，而它们恰恰最需要被看到："有机器用统一密钥
   * 注册上来了"、"某台机器的硬件指纹与一台在线机器相同"。注册发生在配对之前，
   * 那台机器那时还没有场次归属 —— 只能按场次查的话，这些告警会写进库然后
   * 永远没人看见。
   */
  listAll: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<EventOut>(paths.events(), params, signal),

  list: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<EventOut>(paths.contestEvents(contestId), params, signal),

  /**
   * 清空审计日志。
   *
   * `olderThanDays` 是**推荐用法**：日志的价值在于事后回看，一把全清掉很容易
   * 把"三天前那台机器为什么掉线"的唯一线索抹掉。界面上的默认值是一个天数，
   * 而不是"全部"。
   *
   * `confirm` 跟着**范围**走：指定了场次就传场次 `slug`，全局清空传
   * `GLOBAL_CONFIRM`（"all"）—— 因为这次删的不是某一个对象，而是"这一片日志"，
   * 输入范围的名字才有意义。
   */
  clear: (options: {
    confirm: string
    contestId?: number
    level?: string
    olderThanDays?: number
  }) =>
    request<SimpleAck>(
      `${paths.eventsClear()}${query({
        contest_id: options.contestId,
        level: options.level,
        older_than_days: options.olderThanDays,
      })}`,
      { method: 'POST', body: { confirm: options.confirm } },
    ),
}
