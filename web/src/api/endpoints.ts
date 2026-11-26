/**
 * 管理端路径清单。
 *
 * **所有路径只写在这里。** 理由有三个：
 *
 * 1. 服务端改路径时只需要改一处，而不是到十几个视图里搜字符串
 * 2. 参数名（`confirm`、`include_deleted`…）也就跟着只有一处真相
 * 3. 能拿这份清单跟服务端实际注册的路由对账 —— "界面点了没反应"最常见的
 *    原因就是某个路径在服务端已经改名/删掉了，而对账能在跑起来之前就抓到它
 *
 * 命名与形状遵循 `docs/reference/api-conventions.md` §4：集合用复数、嵌套最多两层、
 * 状态变更走子路径动词（`/bind`、`/revoke`）而不是 `PATCH` 一个状态字段。
 *
 * ## 清空全是 `POST .../clear`，不是 `DELETE`
 *
 * "清空"删的是一**批**对象，没有单个名字可以打，所以确认内容是**范围的名字**：
 * 场次范围内的清空用场次 `slug`、名单范围内用名单 `name`；范围本身就是"全部"的
 * （审计日志、待配对机器）用固定字面量 `all`（`schemas.GLOBAL_CONFIRM`）。
 *
 * 确认值走**请求体**而不是 query：它是破坏性动作的唯一闸门，放在 JSON 体里，
 * 浏览器、curl、前端都更不容易在一个 copy-paste 里把它丢掉。
 */

import { query } from './client'

export const ADMIN = '/api/v1/admin'

/**
 * 免登录的全站运行时常量。
 *
 * 它不在 `/api/v1/admin` 下面：登录页、选手页与装机页都要读它，而这三个页面
 * 都拿不到管理端凭据。
 */
export const API_ROOT = '/api/v1'

/** 全局清空时要输入的确认字面量。与服务端 `schemas.GLOBAL_CONFIRM` 对齐。 */
export const GLOBAL_CONFIRM = 'all'

/** 列表接口统一接受的查询参数（`docs/reference/api-conventions.md` §2）。 */
export interface ListQuery {
  limit?: number
  offset?: number
}

/** 把分页参数并进其它查询参数，省掉每个列表都手抄一遍 limit/offset。 */
export function listQuery(params: ListQuery & Record<string, unknown> = {}): string {
  const { limit, offset, ...rest } = params
  return query({ limit, offset, ...(rest as Record<string, string | number | boolean | undefined | null>) })
}

export const paths = {
  // ---- 认证与总览 ----
  login: () => `${ADMIN}/login`,
  logout: () => `${ADMIN}/logout`,
  me: () => `${ADMIN}/me`,
  health: () => `${ADMIN}/health`,

  // ---- 场次 ----
  contests: () => `${ADMIN}/contests`,
  contest: (contestId: number) => `${ADMIN}/contests/${contestId}`,
  /** 归档 / 取消归档（取消归档回到已结束）。 */
  contestArchive: (contestId: number) => `${ADMIN}/contests/${contestId}/archive`,
  contestUnarchive: (contestId: number) => `${ADMIN}/contests/${contestId}/unarchive`,

  // ---- 名单库 ----
  rosters: () => `${ADMIN}/rosters`,
  roster: (rosterId: number) => `${ADMIN}/rosters/${rosterId}`,
  rosterEntries: (rosterId: number) => `${ADMIN}/rosters/${rosterId}/entries`,
  /** 清空名单条目（保留名单本身）。体里要带名单 `name`。 */
  rosterEntriesClear: (rosterId: number) => `${ADMIN}/rosters/${rosterId}/entries/clear`,
  rosterEntry: (entryId: number) => `${ADMIN}/roster-entries/${entryId}`,
  /** 「按姓名匹配机器」：先预览（不写库），确认后应用。 */
  rosterMatchPreview: (rosterId: number) => `${ADMIN}/rosters/${rosterId}/match-machines/preview`,
  rosterMatchApply: (rosterId: number) => `${ADMIN}/rosters/${rosterId}/match-machines/apply`,

  // ---- 场次内的选手 ----
  contestPlayers: (contestId: number) => `${ADMIN}/contests/${contestId}/players`,
  contestPlayersApplyRoster: (contestId: number) =>
    `${ADMIN}/contests/${contestId}/players/apply-roster`,
  /** 清空选手名单。体里要带场次 `slug`。 */
  contestPlayersClear: (contestId: number) => `${ADMIN}/contests/${contestId}/players/clear`,
  player: (playerId: number) => `${ADMIN}/players/${playerId}`,

  // ---- 机器配对 ----
  machinesPending: () => `${ADMIN}/machines/pending`,
  machinePending: (agentId: number) => `${ADMIN}/machines/pending/${agentId}`,
  /** 清空待配对列表。范围是"全部"，所以体里带 `all`。 */
  machinesPendingClear: () => `${ADMIN}/machines/pending/clear`,
  machinesCloneAlerts: () => `${ADMIN}/machines/clone-alerts`,
  /** 已配对的机器（每台带全部名单绑定）。 */
  machinesBound: () => `${ADMIN}/machines/bound`,
  /** 服务端主动通告：GET 读现状，POST 立即喊一次（「召集机器」）。 */
  machinesAnnounce: () => `${ADMIN}/machines/announce`,
  machinesBindByCode: () => `${ADMIN}/machines/bind-by-code`,
  machineBind: (agentId: number) => `${ADMIN}/machines/${agentId}/bind`,
  agent: (agentId: number) => `${ADMIN}/agents/${agentId}`,
  agentBind: (agentId: number) => `${ADMIN}/agents/${agentId}/bind`,
  agentRebind: (agentId: number) => `${ADMIN}/agents/${agentId}/rebind`,
  /** 这台机器在其他名单里的同名建议。 */
  agentBindingSuggestions: (agentId: number) => `${ADMIN}/agents/${agentId}/binding-suggestions`,
  agentContest: (agentId: number) => `${ADMIN}/agents/${agentId}/contest`,
  /** 某台机器**最新一份**诊断包。没收到过是 404 `diagnostics_not_found`。 */
  agentDiagnostics: (agentId: number) => `${ADMIN}/agents/${agentId}/diagnostics`,
  /** 要这台机器下一次心跳回传一份诊断包。一次性标记，它不在线就一直等着。 */
  agentDiagnosticsRequest: (agentId: number) => `${ADMIN}/agents/${agentId}/diagnostics/request`,
  /** 让这台考试机把自己卸载掉。`confirm` 是机器名，令牌随下一次心跳下发。 */
  agentUninstall: (agentId: number) => `${ADMIN}/agents/${agentId}/uninstall`,
  contestAgents: (contestId: number) => `${ADMIN}/contests/${contestId}/agents`,
  /** 运行参数：心跳节奏与离线判定。整间机房生效。 */
  runtimeSettings: () => `${ADMIN}/settings/runtime`,

  // ---- 统一注册密钥 ----
  bootstrapKeys: () => `${ADMIN}/bootstrap-keys`,
  bootstrapKeyRevoke: (keyId: number) => `${ADMIN}/bootstrap-keys/${keyId}/revoke`,
  bootstrapKey: (keyId: number) => `${ADMIN}/bootstrap-keys/${keyId}`,

  // ---- 代码台账 ----
  contestFiles: (contestId: number) => `${ADMIN}/contests/${contestId}/files`,
  /** 导出 zip。默认**含已删除的代码** —— 复核成绩时那份历史比干净的数据重要。 */
  contestFilesExport: (contestId: number) => `${ADMIN}/contests/${contestId}/files/export`,
  /** 清代码台账。体里带场次 `slug` 与 `purge`。 */
  contestFilesClear: (contestId: number) => `${ADMIN}/contests/${contestId}/files/clear`,
  file: (fileId: number) => `${ADMIN}/files/${fileId}`,
  fileContent: (fileId: number) => `${ADMIN}/files/${fileId}/content`,

  // ---- 审计日志 ----
  /** 全局事件：不属于任何场次的告警（统一密钥注册、克隆指纹）只能从这里看到。 */
  events: () => `${ADMIN}/events`,
  /** 清日志。指定了场次就带 `slug`，否则带 `all`。 */
  eventsClear: () => `${ADMIN}/events/clear`,
  contestEvents: (contestId: number) => `${ADMIN}/contests/${contestId}/events`,

  // ---- 文件下发 ----
  contestAssets: (contestId: number) => `${ADMIN}/contests/${contestId}/assets`,
  /** 直接写一个纯文本资产（`须知.txt`、`NOTICE.md`…）。返回与上传同一个 `AssetOut`。 */
  contestAssetsText: (contestId: number) => `${ADMIN}/contests/${contestId}/assets/text`,
  /**
   * 某个资产的正文：GET 读、PUT 覆盖。
   *
   * 同一个路径两个方法，因为它们是同一份东西的两种访问方式（读出来填进对话框、
   * 改完写回去）。能不能改由服务端给的 `AssetOut.editable` 决定 —— 前端不按
   * 扩展名自己判断，两处规则必然分叉。
   */
  assetText: (contestId: number, assetId: number) =>
    `${ADMIN}/contests/${contestId}/assets/${assetId}/text`,
  /**
   * 某个 zip 资产的加密状态（GET）与打密码/打包（POST）。
   *
   * 同一个路径两个方法，和 `assetText` 一样。GET 只读 zip 的标志位（不解压、
   * 不解密任何成员），不是 zip 会拿到 400 `asset_not_zip`；POST 对 zip 是重新
   * 打包同一个 asset id，对非 zip 是**先打包成 zip**（成员名 = 原文件名，资产名
   * 换成 `<原基名>.zip`），两种情况都同步写一份 password.txt。
   *
   * 用的是 InfoZIP 传统加密（ZipCrypto）：学生机上的 Archive Manager 只认这一种。
   * 它是**弱加密** —— 挡得住随手翻看，挡不住有心人。界面文案必须按这个口径写，
   * 不许出现"安全加密"。
   */
  assetZipPassword: (contestId: number, assetId: number) =>
    `${ADMIN}/contests/${contestId}/assets/${assetId}/zip-password`,
  asset: (assetId: number) => `${ADMIN}/assets/${assetId}`,
  contestDeploys: (contestId: number) => `${ADMIN}/contests/${contestId}/deploys`,
  deploy: (taskId: number) => `${ADMIN}/deploys/${taskId}`,
  deployCancel: (taskId: number) => `${ADMIN}/deploys/${taskId}/cancel`,
  deployRetry: (taskId: number) => `${ADMIN}/deploys/${taskId}/retry`,

  // ---- 题目 ----
  contestProblems: (contestId: number) => `${ADMIN}/contests/${contestId}/problems`,
  contestProblemsMatch: (contestId: number) => `${ADMIN}/contests/${contestId}/problems/match`,
  /** 清题目。体里带场次 `slug`，可选 `idents` 缩小范围。 */
  contestProblemsClear: (contestId: number) => `${ADMIN}/contests/${contestId}/problems/clear`,
  problem: (problemId: number) => `${ADMIN}/problems/${problemId}`,
  // ---- 自带评测（§12）----
  problemJudge: (problemId: number) => `${ADMIN}/problems/${problemId}/judge`,
  problemTestdata: (problemId: number) => `${ADMIN}/problems/${problemId}/testdata`,
  contestBuiltinJudge: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/builtin`,
  judgeWorkers: () => `${ADMIN}/judge-workers`,
  /** 「评测服务」全局开关与排队数（§12.10）。 */
  judgeService: () => `${ADMIN}/judge-service`,
  /** 自动入队开关与去抖秒数（§12.9）。 */
  contestJudgeAuto: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/auto`,
  /** 评测任务（任务即历史）。 */
  contestJudgeTasks: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/tasks`,
  contestJudgeTask: (contestId: number, taskId: number) =>
    `${ADMIN}/contests/${contestId}/judge/tasks/${taskId}`,
  /** 采用此版本成绩：把任务结果写成当前 result.json。 */
  /** 评测任务评的那份代码（≤ 1 MB，明细里按行显示，§12.11）。 */
  contestJudgeTaskSource: (contestId: number, taskId: number) =>
    `${ADMIN}/contests/${contestId}/judge/tasks/${taskId}/source`,
  contestJudgeTaskAdopt: (contestId: number, taskId: number) =>
    `${ADMIN}/contests/${contestId}/judge/tasks/${taskId}/adopt`,
  /** 一位选手的代码版本历史与每版评测结果（分数 timeline）。 */
  contestJudgeVersions: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/versions`,
  // ---- 题目评测配置导入 / 导出 ----
  contestJudgeImportPreview: (contestId: number) =>
    `${ADMIN}/contests/${contestId}/judge-import/preview`,
  contestJudgeImportApply: (contestId: number) => `${ADMIN}/contests/${contestId}/judge-import/apply`,
  contestJudgeExportPreview: (contestId: number) =>
    `${ADMIN}/contests/${contestId}/judge-export/preview`,
  contestJudgeExport: (contestId: number) => `${ADMIN}/contests/${contestId}/judge-export`,
  problemJudgeExport: (problemId: number) => `${ADMIN}/problems/${problemId}/judge-export`,
  /** 数据包原样下载（上传时的 zip）。 */
  problemTestdataFile: (problemId: number) => `${ADMIN}/problems/${problemId}/testdata/file`,
  judgeWorker: (workerId: number) => `${ADMIN}/judge-workers/${workerId}`,
  judgeWorkerStop: (workerId: number) => `${ADMIN}/judge-workers/${workerId}/stop`,
  judgeWorkerRestart: (workerId: number) => `${ADMIN}/judge-workers/${workerId}/restart`,
  judgeWorkerUninstall: (workerId: number) => `${ADMIN}/judge-workers/${workerId}/uninstall`,
  /** 本机评测机（服务端子进程，§12.10）。 */
  judgeLocal: () => `${ADMIN}/judge-local`,
  judgeLocalStart: () => `${ADMIN}/judge-local/start`,
  judgeLocalStop: () => `${ADMIN}/judge-local/stop`,

  // ---- 成绩 ----
  contestScores: (contestId: number) => `${ADMIN}/contests/${contestId}/scores`,
  /** 每位选手的交题进度（概览页用，分页与选手列表一致）。 */
  contestScoreProgress: (contestId: number) => `${ADMIN}/contests/${contestId}/scores/progress`,
  contestJudgeRuns: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/runs`,
  contestJudgeRun: (contestId: number, runId: number) =>
    `${ADMIN}/contests/${contestId}/judge/runs/${runId}`,
  contestJudgeRescan: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/rescan`,
  contestJudgeScore: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/score`,
  /** 清成绩记录。体里带场次 `slug`，可选 `player_id` 与 `keep_manual`。 */
  contestJudgeRunsClear: (contestId: number) =>
    `${ADMIN}/contests/${contestId}/judge/runs/clear`,

  // ---- Agent 发布 ----
  releases: () => `${ADMIN}/releases`,
  /** 本机有没有可构建的 Agent 源码、会打出哪个版本。永远 200。 */
  releasesSource: () => `${ADMIN}/releases/source`,
  /** 从本机仓库的 `agent/` 源码构建并签发一个版本（只建草稿，不铺开）。 */
  releasesBuild: () => `${ADMIN}/releases/build`,
  releaseRollout: (releaseId: number) => `${ADMIN}/releases/${releaseId}/rollout`,
  releaseYank: (releaseId: number) => `${ADMIN}/releases/${releaseId}/yank`,
  release: (releaseId: number) => `${ADMIN}/releases/${releaseId}`,

  // ---- 装机入口（**免登录**）----
  // 不在 `/api/v1/admin` 下面，因为调用者不是登录后的管理界面，而是"任意一台还没
  // 装 Agent 的机器"和站在它前面的教师。**这里也放考生页的接口** —— 它同样
  // 不要管理端凭据：身份靠本机来源 IP，或「准考证号 + 场次考生页密码」换来的令牌。
  installLedger: () => '/api/v1/agent/install.json',
  installBundle: () => '/api/v1/agent/install/bundle',
  installInstaller: () => '/api/v1/agent/install/installer',
  installBootstrap: () => '/api/v1/agent/install/bootstrap.sh',
  playerContext: () => '/api/v1/player/context',
  playerPreview: () => '/api/v1/player/preview',
  playerLogin: () => '/api/v1/player/login',
  playerAsset: (assetId: number) => `/api/v1/player/assets/${assetId}`,
  /** 全站运行时常量（显示时区…）。**免登录**，三个前台页面都要读它。 */
  meta: () => `${API_ROOT}/meta`,
} as const
