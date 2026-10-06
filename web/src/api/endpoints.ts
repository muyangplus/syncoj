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
 * 命名与形状遵循 `docs/api-conventions.md` §4：集合用复数、嵌套最多两层、
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

/** 全局清空时要输入的确认字面量。与服务端 `schemas.GLOBAL_CONFIRM` 对齐。 */
export const GLOBAL_CONFIRM = 'all'

/** 列表接口统一接受的查询参数（`docs/api-conventions.md` §2）。 */
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

  // ---- 名单库 ----
  rosters: () => `${ADMIN}/rosters`,
  roster: (rosterId: number) => `${ADMIN}/rosters/${rosterId}`,
  rosterEntries: (rosterId: number) => `${ADMIN}/rosters/${rosterId}/entries`,
  /** 清空名单条目（保留名单本身）。体里要带名单 `name`。 */
  rosterEntriesClear: (rosterId: number) => `${ADMIN}/rosters/${rosterId}/entries/clear`,
  rosterEntry: (entryId: number) => `${ADMIN}/roster-entries/${entryId}`,

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
  machinesBindByCode: () => `${ADMIN}/machines/bind-by-code`,
  machineBind: (agentId: number) => `${ADMIN}/machines/${agentId}/bind`,
  agent: (agentId: number) => `${ADMIN}/agents/${agentId}`,
  agentBind: (agentId: number) => `${ADMIN}/agents/${agentId}/bind`,
  agentRebind: (agentId: number) => `${ADMIN}/agents/${agentId}/rebind`,
  agentContest: (agentId: number) => `${ADMIN}/agents/${agentId}/contest`,
  contestAgents: (contestId: number) => `${ADMIN}/contests/${contestId}/agents`,

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
   * 某个 zip 资产的加密状态（GET）与打密码/改密码（POST）。
   *
   * 同一个路径两个方法，和 `assetText` 一样。GET 只读 zip 的标志位（不解压、
   * 不解密任何成员）；POST 重新打包同一个 asset id，并同步写一份 password.txt。
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

  // ---- 成绩 ----
  contestScores: (contestId: number) => `${ADMIN}/contests/${contestId}/scores`,
  contestJudgeRuns: (contestId: number) => `${ADMIN}/contests/${contestId}/judge/runs`,
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
  // 装 Agent 的机器"和站在它前面的教师。**这里也放选手页的只读接口** —— 它同样
  // 免登录（选手不登录），身份靠场次 slug + 考号。
  installLedger: () => '/api/v1/agent/install.json',
  installBundle: () => '/api/v1/agent/install/bundle',
  installInstaller: () => '/api/v1/agent/install/installer',
  installBootstrap: () => '/api/v1/agent/install/bootstrap.sh',
  playerContext: () => '/api/v1/player/context',
} as const
