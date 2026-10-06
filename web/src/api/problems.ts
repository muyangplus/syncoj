/** 题目清单与归题。 */

import { request } from './client'
import { clearCollection, listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { ProblemImportOut, ProblemMatchOut, ProblemOut, ProblemUpsert } from './types'

export const problemApi = {
  list: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<ProblemOut>(paths.contestProblems(contestId), { limit: 500, ...params }, signal),

  /** 批量登记 / 更新。按 ident 幂等 —— 清单可以反复导。 */
  import: (contestId: number, problems: ProblemUpsert[]) =>
    request<ProblemImportOut>(paths.contestProblems(contestId), {
      method: 'POST',
      body: problems,
    }),

  update: (problemId: number, payload: ProblemUpsert) =>
    request<ProblemOut>(paths.problem(problemId), { method: 'PATCH', body: payload }),

  /** 硬删除。`confirm` 必须逐字等于题目标识（ident）。 */
  remove: (problemId: number, ident: string) => removeItem(paths.problem(problemId), ident),

  clear: (contestId: number, slug: string) =>
    clearCollection(paths.contestProblemsClear(contestId), slug),

  /**
   * 试算一条相对路径会归到哪道题。
   *
   * 走服务端的同一份匹配实现 —— 界面自己算一遍迟早会和服务端说不一致。
   */
  match: (contestId: number, path: string) =>
    request<ProblemMatchOut>(paths.contestProblemsMatch(contestId), {
      method: 'POST',
      body: { path },
    }),
}
