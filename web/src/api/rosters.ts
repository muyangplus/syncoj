/** 名单库与名单条目。 */

import { request } from './client'
import { clearCollection, listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { ApplyRosterIn, ApplyRosterOut, RosterCreate, RosterDetailOut, RosterEntryIn, RosterEntryOut, RosterImportOut, RosterOut } from './types'

export const rosterApi = {
  list: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<RosterOut>(paths.rosters(), { limit: 500, ...params }, signal),

  get: (rosterId: number) => request<RosterDetailOut>(paths.roster(rosterId)),

  create: (payload: RosterCreate) =>
    request<RosterOut>(paths.rosters(), { method: 'POST', body: payload }),

  update: (rosterId: number, payload: RosterCreate) =>
    request<RosterOut>(paths.roster(rosterId), { method: 'PATCH', body: payload }),

  /** 硬删除。`confirm` 必须逐字等于名单名称。 */
  remove: (rosterId: number, name: string) => removeItem(paths.roster(rosterId), name),

  /** 批量登记/更新条目。按 player_no 幂等，可以反复导。 */
  importEntries: (rosterId: number, entries: RosterEntryIn[]) =>
    request<RosterImportOut>(paths.rosterEntries(rosterId), {
      method: 'POST',
      body: entries,
    }),

  updateEntry: (entryId: number, payload: RosterEntryIn) =>
    request<RosterEntryOut>(paths.rosterEntry(entryId), { method: 'PATCH', body: payload }),

  /** 硬删除。`confirm` 是这条的**考号** —— 服务端逐字校验。 */
  removeEntry: (entryId: number, playerNo: string) =>
    removeItem(paths.rosterEntry(entryId), playerNo),

  /** 清空名单里的全部条目，保留名单本身。`confirm` 是名单名称。 */
  clearEntries: (rosterId: number, name: string) =>
    clearCollection(paths.rosterEntriesClear(rosterId), name),

  /** 把名单应用到场次：补人、更新，默认不删。 */
  applyToContest: (contestId: number, payload: ApplyRosterIn) =>
    request<ApplyRosterOut>(paths.contestPlayersApplyRoster(contestId), {
      method: 'POST',
      body: payload,
    }),
}
