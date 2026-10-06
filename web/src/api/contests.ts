/** 场次。 */

import { request } from './client'
import { listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { ContestCreate, ContestOut, ContestUpdate } from './types'

export const contestApi = {
  /** 场次是天然很小的集合，但仍然走信封 —— 前端只维护一套分页逻辑。 */
  list: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<ContestOut>(paths.contests(), { limit: 500, ...params }, signal),

  create: (payload: ContestCreate) =>
    request<ContestOut>(paths.contests(), { method: 'POST', body: payload }),

  /** 只改传了的字段。清空默认名单要用 `clear_default_roster`。 */
  update: (contestId: number, payload: ContestUpdate) =>
    request<ContestOut>(paths.contest(contestId), { method: 'PATCH', body: payload }),

  /** 硬删除。`confirm` 必须逐字等于场次标识（slug）。 */
  remove: (contestId: number, slug: string) =>
    removeItem(paths.contest(contestId), slug),
}
