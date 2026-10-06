/** 场次内的选手。 */

import { request } from './client'
import { clearCollection, listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { PlayerImportOut, PlayerOut, PlayerUpsert } from './types'

export const playerApi = {
  list: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<PlayerOut>(paths.contestPlayers(contestId), params, signal),

  /**
   * 批量导入/更新。按 `player_no` 幂等 upsert。
   *
   * 返回的是**动作结果**（`created` / `updated` / `players`），不是列表信封 ——
   * 信封只属于 GET 集合。`players` 让界面拿到新建行的 id，好接着做"给这几个人
   * 应用名单"之类的动作。
   */
  import: (contestId: number, players: PlayerUpsert[]) =>
    request<PlayerImportOut>(paths.contestPlayers(contestId), {
      method: 'POST',
      body: players,
    }),

  update: (playerId: number, payload: PlayerUpsert) =>
    request<PlayerOut>(paths.player(playerId), { method: 'PATCH', body: payload }),

  /** 硬删除，连同代码台账与成绩（级联）。`confirm` 是考号。 */
  remove: (playerId: number, playerNo: string) => removeItem(paths.player(playerId), playerNo),

  /**
   * 清空本场次选手。`confirm` 是**场次标识**。
   *
   * `keepWithSubmissions` 默认 true —— 已经有代码或成绩的选手留着，
   * 因为顺手把提交一起删掉是不可逆的。真要连提交一起清必须显式传 false。
   */
  clear: (contestId: number, slug: string, keepWithSubmissions = true) =>
    clearCollection(paths.contestPlayersClear(contestId), slug, {
      keep_with_submissions: keepWithSubmissions,
    }),
}
