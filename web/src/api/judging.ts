/**
 * 自带沙箱评测（§12）：题目的评测配置与数据包、排任务与进度、评测机令牌。
 *
 * 评测本身不在服务端进程里跑：独立账号的评测机（`syncoj-judge`）经 HTTP 领任务，
 * 结果写进 `judge_result/`，由成绩扫描收进矩阵 —— 所以成绩照旧从 `scoreApi` 读。
 */

import { query, request } from './client'
import { listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type {
  BuiltinJudgeIn,
  BuiltinJudgeProgressOut,
  BuiltinJudgeQueuedOut,
  JudgeAutoIn,
  JudgeAutoOut,
  JudgeRunDetailOut,
  JudgeServiceIn,
  JudgeServiceOut,
  JudgeSourceOut,
  JudgeTaskDetailOut,
  JudgeTaskOut,
  JudgeWorkerCreatedOut,
  JudgeWorkerOut,
  JudgeWorkerPatchIn,
  LocalJudgeOut,
  ProblemJudgeIn,
  ProblemOut,
  SimpleAck,
  SourceVersionOut,
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

  /** 自动入队：`on_change` = 收到某题新版本就评，`auto_judge_delay` 秒内的连续改动只评最后一版。 */
  setAuto: (contestId: number, payload: JudgeAutoIn) =>
    request<JudgeAutoOut>(paths.contestJudgeAuto(contestId), { method: 'PUT', body: payload }),

  /** 评测任务，新的在前；可按选手、题目、状态、触发方式、风险标注（`flag`）筛。 */
  tasks: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<JudgeTaskOut>(paths.contestJudgeTasks(contestId), params, signal),

  /** 一个任务及其测试点明细（历史版本的结果从这里看）。 */
  task: (contestId: number, taskId: number) =>
    request<JudgeTaskDetailOut>(paths.contestJudgeTask(contestId, taskId)),

  /** 这次评测的代码（明细里按行显示、标注行高亮）。超过 1 MB 413，没领到代码 404。 */
  taskSource: (contestId: number, taskId: number) =>
    request<JudgeSourceOut>(paths.contestJudgeTaskSource(contestId, taskId)),

  /** 采用此版本成绩：成绩矩阵这一格换成该任务的结果。手工录入的格子 409 `manual_score`。 */
  adopt: (contestId: number, taskId: number) =>
    request<JudgeTaskOut>(paths.contestJudgeTaskAdopt(contestId, taskId), { method: 'POST' }),

  /** 一位选手的代码版本（旧的在前），每版带最近一次评测结果。`problem` 只看归到这道题的。 */
  versions: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<SourceVersionOut>(paths.contestJudgeVersions(contestId), { limit: 500, ...params }, signal),

  /** 一条评测记录与测试点明细。 */
  run: (contestId: number, runId: number) =>
    request<JudgeRunDetailOut>(paths.contestJudgeRun(contestId, runId)),

  workers: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<JudgeWorkerOut>(paths.judgeWorkers(), { limit: 100, ...params }, signal),

  /** 签发评测机令牌：明文只在这一次返回，连同 Linux / Windows 一行安装命令。 */
  createWorker: (name: string) =>
    request<JudgeWorkerCreatedOut>(paths.judgeWorkers(), { method: 'POST', body: { name } }),

  /**
   * 改名；暂停（`enabled: false`，领不到新任务、照常心跳）/ 恢复；改并发数（`slots`，评完手上的
   * 任务后以新值重启）。非 isolate 改成大于 1 时 409 `judge_worker_slots`，旧评测机 409。
   */
  updateWorker: (workerId: number, payload: JudgeWorkerPatchIn) =>
    request<JudgeWorkerOut>(paths.judgeWorker(workerId), { method: 'PATCH', body: payload }),

  /** 停止：下一次轮询送达，评完手上的任务后退出。离线 / 旧评测机 409。 */
  stopWorker: (workerId: number) =>
    request<JudgeWorkerOut>(paths.judgeWorkerStop(workerId), { method: 'POST' }),

  /** 重启：评完手上的任务后以同样的参数重启。 */
  restartWorker: (workerId: number) =>
    request<JudgeWorkerOut>(paths.judgeWorkerRestart(workerId), { method: 'POST' }),

  /**
   * 卸载：评测机评完手上的任务后告别（服务端删行、令牌失效），再删掉自己的服务 / 计划任务、程序、
   * 令牌与缓存。`confirm` 必须逐字等于评测机名称。离线 / 不支持卸载的旧评测机 409；
   * 本机评测机没在跑时直接删。
   */
  uninstallWorker: (workerId: number, name: string) =>
    request<SimpleAck>(`${paths.judgeWorkerUninstall(workerId)}${query({ confirm: name })}`, {
      method: 'POST',
    }),

  /** 「评测服务」开关与全部场次的排队 / 在评数。 */
  service: () => request<JudgeServiceOut>(paths.judgeService()),

  /** 关 = 所有评测机领不到新任务（手上的照常评完，排队的留着）。 */
  setService: (payload: JudgeServiceIn) =>
    request<JudgeServiceOut>(paths.judgeService(), { method: 'PUT', body: payload }),

  /** 本机评测机状态；非 Linux 服务端 `supported: false`。 */
  localJudge: () => request<LocalJudgeOut>(paths.judgeLocal()),

  startLocalJudge: () => request<LocalJudgeOut>(paths.judgeLocalStart(), { method: 'POST' }),

  stopLocalJudge: () => request<LocalJudgeOut>(paths.judgeLocalStop(), { method: 'POST' }),

  /** 吊销（删行）。`confirm` 必须逐字等于评测机名称。 */
  removeWorker: (workerId: number, name: string) => removeItem(paths.judgeWorker(workerId), name),
}
