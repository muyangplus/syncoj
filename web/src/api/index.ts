/**
 * 管理端接口。
 *
 * 每个资源一个对象，方法名就是动作名。分页、信封、`confirm`、错误体全部由
 * `crud.ts` / `endpoints.ts` 吃掉，这里只做"路径 + 参数 + 返回类型"的映射 ——
 * 业务逻辑不放这里（那属于页面或 store）。
 *
 * **列表方法统一是 `(key, params?, signal?)`**：`key` 是父资源标识（大多数
 * 情况下是场次 id），`params` 里放分页与筛选，`signal` 用来取消过期请求。
 * 形状统一的好处是页面里那句 `useList` 的 loader 写到哪里都一样。
 */

import { download, downloadWithServerName, fetchText, query, request } from './client'
import { clearCollection, listPage, removeItem, softRemoveItem } from './crud'
import type { ListQuery, Params } from './crud'
import { GLOBAL_CONFIRM, paths } from './endpoints'
import type {
  AdminInfo,
  AgentRuntimeOut,
  ApplyRosterIn,
  ApplyRosterOut,
  AssetOut,
  AssetRenameIn,
  BindResultOut,
  BootstrapKeyIssueIn,
  BootstrapKeyIssuedOut,
  BootstrapKeyOut,
  CloneAlertOut,
  ContestCreate,
  ContestOut,
  ContestUpdate,
  DeployCreate,
  DeployTaskOut,
  EventOut,
  JudgeRunOut,
  JudgeScanOut,
  LoginResponse,
  ManualScoreIn,
  PendingMachineOut,
  PlayerImportOut,
  PlayerOut,
  PlayerUpsert,
  ProblemImportOut,
  ProblemMatchOut,
  ProblemOut,
  ProblemUpsert,
  ReleaseOut,
  ReleaseUpdate,
  RosterCreate,
  RosterDetailOut,
  RosterEntryIn,
  RosterEntryOut,
  RosterImportOut,
  RosterOut,
  ScoreMatrixOut,
  SimpleAck,
  SourceFileOut,
  UpgradeStatusOut,
} from './types'

/** 列表方法的入参：分页 + 该资源自己的筛选条件。 */
export type ListParams = ListQuery & Params

export const authApi = {
  login: (username: string, password: string) =>
    request<LoginResponse>(paths.login(), {
      method: 'POST',
      body: { username, password },
    }),

  logout: () => request<SimpleAck>(paths.logout(), { method: 'POST' }),

  me: () => request<AdminInfo>(paths.me()),

  /**
   * 管理端健康检查。
   *
   * 用它而不是 `/healthz`：那个是不带鉴权的探活接口，只回答"进程还在不在"，
   * 而这个还会回机器数量，概览页的状态卡片要的就是它。
   */
  health: () =>
    request<AdminHealth>(paths.health()),
}

/** `/admin/health` 的响应。字段不多，且都是概览卡片直接要用的。 */
export interface AdminHealth {
  ok: boolean
  agents_total: number
  agents_online: number
  data_root: string
}

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

/** 某场次里的机器（已经配对到人的那些）。 */
export const agentApi = {
  list: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<AgentRuntimeOut>(paths.contestAgents(contestId), { limit: 500, ...params }, signal),

  /** 改派给名单里的另一个人。机器不用重启，下一轮心跳就换身份。 */
  rebind: (agentId: number, rosterEntryId: number) =>
    request<SimpleAck>(paths.agentRebind(agentId), {
      method: 'POST',
      body: { roster_entry_id: rosterEntryId },
    }),

  /** 指定场次。`null` = 回到自动解析。 */
  setContest: (agentId: number, contestId: number | null) =>
    request<SimpleAck>(paths.agentContest(agentId), {
      method: 'POST',
      body: { contest_id: contestId },
    }),

  /** 解除绑定：机器还回来，人留着。它下一轮心跳会显示新的配对码。 */
  unbind: (agentId: number) => request<SimpleAck>(paths.agentBind(agentId), { method: 'DELETE' }),

  /**
   * 作废一台机器的凭据。
   *
   * 硬删除机器，所以 `confirm` 是主机名。作废之后那台机器下次心跳拿 401，
   * 需要重新注册并配对 —— 这是"这台机器不再属于我们"的表达。
   */
  revoke: (agentId: number, hostname: string) =>
    removeItem(paths.agent(agentId), hostname),
}

/**
 * 机器配对。
 *
 * 统一密钥注册上来的机器**没有归属**（服务端还不知道它是谁），靠这里的几个
 * 接口把它认领到名单里的**人**。配对是永久的：绑的是 `roster_entry`，
 * 不是某场比赛的选手，所以同一个学生换一场比赛不用重新配。
 */
export const machineApi = {
  /** 待配对的机器。按最后心跳倒序 —— 教师站在机器前时它就在最上面。 */
  pending: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<PendingMachineOut>(paths.machinesPending(), { limit: 500, ...params }, signal),

  /** 疑似克隆镜像：多台机器共用同一个硬件指纹。 */
  cloneAlerts: () => request<CloneAlertOut[]>(paths.machinesCloneAlerts()),

  /** 按配对码配对：机器上显示什么就输什么。六位数字，用一次即作废。 */
  bindByCode: (pairCode: string, rosterEntryId: number) =>
    request<BindResultOut>(paths.machinesBindByCode(), {
      method: 'POST',
      body: { pair_code: pairCode, roster_entry_id: rosterEntryId },
    }),

  /** 从待配对列表里点选。给了配对码就必须对得上。 */
  bind: (agentId: number, rosterEntryId: number, pairCode?: string) =>
    request<BindResultOut>(paths.machineBind(agentId), {
      method: 'POST',
      body: { roster_entry_id: rosterEntryId, pair_code: pairCode || null },
    }),

  /** 把一台待配对的机器从列表里去掉（认错机器、测试机、刷注册的垃圾）。 */
  revokePending: (agentId: number, hostname?: string) =>
    removeItem(paths.machinePending(agentId), hostname),

  /**
   * 清空待配对列表。
   *
   * 范围是"全部"，所以 `confirm` 是固定字面量 `all`（`GLOBAL_CONFIRM`）——
   * 这里没有"一个名字"可打，一次要清掉的是 N 台机器，输入**范围的名字**才是
   * 这个约定想表达的东西。
   *
   * 界面必须同时提醒**先吊销统一密钥**，否则那批机器开机还会回来。
   */
  clearPending: (confirm: string = GLOBAL_CONFIRM) =>
    clearCollection(paths.machinesPendingClear(), confirm),
}

/** 镜像内置的统一注册密钥 */
export const bootstrapKeyApi = {
  list: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<BootstrapKeyOut>(paths.bootstrapKeys(), { limit: 500, ...params }, signal),

  /** 明文**只返回这一次**，之后库里只有哈希 —— 界面必须当场显示给教师抄。 */
  issue: (payload: BootstrapKeyIssueIn) =>
    request<BootstrapKeyIssuedOut>(paths.bootstrapKeys(), {
      method: 'POST',
      body: payload,
    }),

  revoke: (keyId: number) =>
    request<BootstrapKeyOut>(paths.bootstrapKeyRevoke(keyId), { method: 'POST' }),

  /** 彻底删掉。只允许删"签错了、一次都没用过"的密钥。 */
  remove: (keyId: number, label?: string) => removeItem(paths.bootstrapKey(keyId), label),
}

export const fileApi = {
  list: (
    contestId: number,
    params: ListParams & { playerId?: number; includeDeleted?: boolean } = {},
    signal?: AbortSignal,
  ) => {
    const { playerId, includeDeleted, ...rest } = params
    return listPage<SourceFileOut>(
      paths.contestFiles(contestId),
      { include_deleted: includeDeleted, player_id: playerId, ...rest },
      signal,
    )
  },

  /**
   * 导出 zip：`代码/<考号>/<路径>` 加一份带**精确字节数与 SHA256** 的清单。
   *
   * 默认 `include_deleted=true`：考场现实是"选手确实交过，只是后来被清了"，
   * 复核成绩时这份历史比干净的数据重要。
   */
  exportZip: (
    contestId: number,
    options: { playerId?: number; problem?: string; includeDeleted?: boolean } = {},
  ) =>
    downloadWithServerName(
      `${paths.contestFilesExport(contestId)}${query({
        player_id: options.playerId,
        problem: options.problem,
        include_deleted: options.includeDeleted ?? true,
      })}`,
      `代码归档-${contestId}.zip`,
    ),

  /** 取一份代码的正文。用来在界面上直接看，不落地到磁盘。 */
  content: (fileId: number) => fetchText(paths.fileContent(fileId)),

  /** 下载一份代码。服务端会按相对路径的 basename 给文件名。 */
  download: (fileId: number, fallbackName: string) =>
    download(paths.fileContent(fileId), fallbackName),

  /** 软删除（墓碑）。行还在，`include_deleted=true` 时还能看到。 */
  remove: (fileId: number) => softRemoveItem(paths.file(fileId)),

  /**
   * 清空台账。`confirm` 是**场次标识**。
   *
   * `purge=false`（默认）只清已经消失的墓碑记录 —— 不影响任何还在的东西。
   * `purge=true` 会连没消失的一起删，但机器还在报的文件下一轮就会回来。
   */
  clear: (
    contestId: number,
    slug: string,
    options: { purge?: boolean; playerId?: number } = {},
  ) =>
    request<SimpleAck>(
      // `player_id` 是查询参数（缩小范围），`confirm` 与 `purge` 是请求体
      `${paths.contestFilesClear(contestId)}${query({ player_id: options.playerId })}`,
      { method: 'POST', body: { confirm: slug, purge: options.purge ?? false } },
    ),
}

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

export const assetApi = {
  list: (
    contestId: number,
    params: ListParams = {},
    signal?: AbortSignal,
  ) => listPage<AssetOut>(paths.contestAssets(contestId), { limit: 500, ...params }, signal),

  /**
   * 上传一个待下发文件。
   *
   * 服务端**原样落盘、不解压**：题面与样例都是 zip，密码由教师当成普通资产
   * 一起下发（`password.txt`）。系统不碰密码，也不需要知道它。
   */
  upload: (contestId: number, file: File, kind = 'testdata') => {
    const form = new FormData()
    form.append('file', file)
    form.append('kind', kind)
    return request<AssetOut>(paths.contestAssets(contestId), { method: 'POST', form })
  },

  /** 改名。内容按 sha256 存，改名只换标签 —— 未完成的下发任务会按新名字落地。 */
  rename: (assetId: number, payload: AssetRenameIn) =>
    request<AssetOut>(paths.asset(assetId), { method: 'PATCH', body: payload }),

  /** 软删除（墓碑）。已经落到选手机器上的文件不会撤回。 */
  remove: (assetId: number) => softRemoveItem(paths.asset(assetId)),
}

export const deployApi = {
  list: (
    contestId: number,
    params: ListParams & { includeTargets?: boolean } = {},
    signal?: AbortSignal,
  ) => {
    const { includeTargets, ...rest } = params
    return listPage<DeployTaskOut>(
      paths.contestDeploys(contestId),
      { include_targets: includeTargets, ...rest },
      signal,
    )
  },

  get: (taskId: number) => request<DeployTaskOut>(paths.deploy(taskId)),

  create: (contestId: number, payload: DeployCreate) =>
    request<DeployTaskOut>(paths.contestDeploys(contestId), { method: 'POST', body: payload }),

  cancel: (taskId: number) =>
    request<SimpleAck>(paths.deployCancel(taskId), { method: 'POST' }),

  retry: (taskId: number) =>
    request<SimpleAck>(paths.deployRetry(taskId), { method: 'POST' }),

  remove: (taskId: number) => removeItem(paths.deploy(taskId)),
}

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

export const releaseApi = {
  status: () => request<UpgradeStatusOut>(paths.releases()),

  upload: (file: File, version: string, notes: string, channel = 'stable') => {
    const form = new FormData()
    form.append('file', file)
    form.append('version', version)
    form.append('notes', notes)
    form.append('channel', channel)
    return request<ReleaseOut>(paths.releases(), { method: 'POST', form })
  },

  rollout: (releaseId: number) =>
    request<ReleaseOut>(paths.releaseRollout(releaseId), { method: 'POST' }),

  yank: (releaseId: number) =>
    request<ReleaseOut>(paths.releaseYank(releaseId), { method: 'POST' }),

  update: (releaseId: number, payload: ReleaseUpdate) =>
    request<ReleaseOut>(paths.release(releaseId), { method: 'PATCH', body: payload }),

  remove: (releaseId: number) => removeItem(paths.release(releaseId)),
}
