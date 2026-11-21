/**
 * 自带沙箱评测（§12）：题目的评测配置与数据包、排任务与进度、评测机令牌。
 *
 * 评测本身不在服务端进程里跑：独立账号的评测机（`syncoj-judge`）经 HTTP 领任务，
 * 结果写进 `judge_result/`，由成绩扫描收进矩阵 —— 所以成绩照旧从 `scoreApi` 读。
 */

import { request } from './client'
import { listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type {
  BuiltinJudgeIn,
  BuiltinJudgeProgressOut,
  BuiltinJudgeQueuedOut,
  JudgeWorkerCreatedOut,
  JudgeWorkerOut,
  ProblemJudgeIn,
  ProblemOut,
} from './types'

export const judgingApi = {
  /** 题目的评测方式与时限、内存、IO、编译参数。编译参数只收白名单（服务端校验）。 */
  setProblemJudge: (problemId: number, payload: ProblemJudgeIn) =>
    request<ProblemOut>(paths.problemJudge(problemId), { method: 'PUT', body: payload }),

  /** 上传数据包（zip）。结构不对（缺答案、路径不安全、配置写错）当场 400。 */
  uploadTestdata: (problemId: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<ProblemOut>(paths.problemTestdata(problemId), { method: 'POST', form })
  },

  removeTestdata: (problemId: number) =>
    request<ProblemOut>(paths.problemTestdata(problemId), { method: 'DELETE' }),

  /**
   * 排评测任务（已有的会被替换 = 重测）。场次还在进行时 409 `contest_in_progress`，
   * 教师确认后带 `force: true` 再来。
   */
  queue: (contestId: number, payload: BuiltinJudgeIn) =>
    request<BuiltinJudgeQueuedOut>(paths.contestBuiltinJudge(contestId), { method: 'POST', body: payload }),

  progress: (contestId: number) => request<BuiltinJudgeProgressOut>(paths.contestBuiltinJudge(contestId)),

  workers: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<JudgeWorkerOut>(paths.judgeWorkers(), { limit: 100, ...params }, signal),

  /** 签发评测机令牌：明文只在这一次返回。 */
  createWorker: (name: string) =>
    request<JudgeWorkerCreatedOut>(paths.judgeWorkers(), { method: 'POST', body: { name } }),

  /** 吊销（删行）。`confirm` 必须逐字等于评测机名称。 */
  removeWorker: (workerId: number, name: string) => removeItem(paths.judgeWorker(workerId), name),
}
