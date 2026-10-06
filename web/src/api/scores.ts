/** 成绩矩阵与评测记录。 */

import { query, request } from './client'
import { clearCollection, listPage } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { JudgeRunOut, JudgeScanOut, ManualScoreIn, ScoreMatrixOut, SimpleAck } from './types'

export const scoreApi = {
  matrix: (contestId: number) => request<ScoreMatrixOut>(paths.contestScores(contestId)),

  runs: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<JudgeRunOut>(paths.contestJudgeRuns(contestId), params, signal),

  rescan: (contestId: number) =>
    request<JudgeScanOut>(paths.contestJudgeRescan(contestId), { method: 'POST' }),

  setManual: (contestId: number, payload: ManualScoreIn) =>
    request<JudgeRunOut>(paths.contestJudgeScore(contestId), {
      method: 'PUT',
      body: payload,
    }),

  clear: (contestId: number, playerId: number, problem: string, playerNo: string) =>
    request<SimpleAck>(
      // `confirm` 是**考号** —— 清除一个格子的成绩等于抹掉一个人的一道题
      `${paths.contestJudgeScore(contestId)}${query({
        player_id: playerId,
        problem,
        confirm: playerNo,
      })}`,
      { method: 'DELETE' },
    ),

  /**
   * 清空成绩记录。`confirm` 是场次标识。
   *
   * `keepManual` 默认 true，因为手工录入的分数是全场最贵的数据 —— 不该被
   * "重扫一遍"顺手清掉。`playerId` 把范围缩小到一个人。
   */
  clearRuns: (
    contestId: number,
    slug: string,
    options: { playerId?: number; keepManual?: boolean } = {},
  ) =>
    clearCollection(paths.contestJudgeRunsClear(contestId), slug, {
      player_id: options.playerId,
      keep_manual: options.keepManual ?? true,
    }),
}
