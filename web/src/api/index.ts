/**
 * 类型化的接口封装。
 *
 * 这里只做"路径 + 参数 + 返回类型"的映射，不放业务逻辑 —— 业务逻辑在 store
 * 或视图里。保持这一层足够薄，接口变动时才只需要改一处。
 */

import { query, request } from './client'
import type {
  AdminInfo,
  AgentRuntimeOut,
  ApplyRosterIn,
  ApplyRosterOut,
  AssetOut,
  ContestCreate,
  ContestOut,
  ContestUpdate,
  DeployCreate,
  DeployTaskOut,
  EnrollCodeOut,
  EventOut,
  JudgeRunOut,
  JudgeScanOut,
  LoginResponse,
  ManualScoreIn,
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
  RosterImportOut,
  RosterOut,
  ScoreMatrixOut,
  SimpleAck,
  SourceFileOut,
  UpgradeStatusOut,
} from './types'

const ADMIN = '/api/v1/admin'

export const authApi = {
  login: (username: string, password: string) =>
    request<LoginResponse>(`${ADMIN}/login`, {
      method: 'POST',
      body: { username, password },
    }),

  logout: () => request<SimpleAck>(`${ADMIN}/logout`, { method: 'POST' }),

  me: () => request<AdminInfo>(`${ADMIN}/me`),

  health: () =>
    request<{
      ok: boolean
      agents_total: number
      agents_online: number
      data_root: string
    }>('/healthz'),
}

export const contestApi = {
  list: () => request<ContestOut[]>(`${ADMIN}/contests`),

  create: (payload: ContestCreate) =>
    request<ContestOut>(`${ADMIN}/contests`, { method: 'POST', body: payload }),

  /** 只改传了的字段。清空默认名单要用 `clear_default_roster`。 */
  update: (contestId: number, payload: ContestUpdate) =>
    request<ContestOut>(`${ADMIN}/contests/${contestId}`, {
      method: 'PATCH',
      body: payload,
    }),
}

export const rosterApi = {
  list: () => request<RosterOut[]>(`${ADMIN}/rosters`),

  get: (rosterId: number) => request<RosterDetailOut>(`${ADMIN}/rosters/${rosterId}`),

  create: (payload: RosterCreate) =>
    request<RosterOut>(`${ADMIN}/rosters`, { method: 'POST', body: payload }),

  update: (rosterId: number, payload: RosterCreate) =>
    request<RosterOut>(`${ADMIN}/rosters/${rosterId}`, {
      method: 'PATCH',
      body: payload,
    }),

  remove: (rosterId: number) =>
    request<SimpleAck>(`${ADMIN}/rosters/${rosterId}`, { method: 'DELETE' }),

  /** 批量登记/更新条目。按 player_no 幂等，可以反复导。 */
  importEntries: (rosterId: number, entries: RosterEntryIn[]) =>
    request<RosterImportOut>(`${ADMIN}/rosters/${rosterId}/entries`, {
      method: 'POST',
      body: entries,
    }),

  removeEntry: (entryId: number) =>
    request<SimpleAck>(`${ADMIN}/roster-entries/${entryId}`, { method: 'DELETE' }),

  /** 把名单应用到场次：补人、更新，默认不删。 */
  applyToContest: (contestId: number, payload: ApplyRosterIn) =>
    request<ApplyRosterOut>(`${ADMIN}/contests/${contestId}/players/apply-roster`, {
      method: 'POST',
      body: payload,
    }),
}

export const playerApi = {
  list: (contestId: number) => request<PlayerOut[]>(`${ADMIN}/contests/${contestId}/players`),

  import: (contestId: number, players: PlayerUpsert[]) =>
    request<PlayerOut[]>(`${ADMIN}/contests/${contestId}/players`, {
      method: 'POST',
      body: players,
    }),

  issueEnrollCode: (playerId: number) =>
    request<EnrollCodeOut>(`${ADMIN}/players/${playerId}/enroll-code`, { method: 'POST' }),
}

export const problemApi = {
  list: (contestId: number) =>
    request<ProblemOut[]>(`${ADMIN}/contests/${contestId}/problems`),

  /** 批量登记 / 更新。按 ident 幂等 —— 清单可以反复导。 */
  import: (contestId: number, problems: ProblemUpsert[]) =>
    request<ProblemImportOut>(`${ADMIN}/contests/${contestId}/problems`, {
      method: 'POST',
      body: problems,
    }),

  update: (problemId: number, payload: ProblemUpsert) =>
    request<ProblemOut>(`${ADMIN}/problems/${problemId}`, {
      method: 'PATCH',
      body: payload,
    }),

  remove: (problemId: number) =>
    request<SimpleAck>(`${ADMIN}/problems/${problemId}`, { method: 'DELETE' }),

  /**
   * 试算一条相对路径会归到哪道题。
   *
   * 走服务端的同一份匹配实现 —— 界面自己算一遍迟早会和服务端说不一致。
   */
  match: (contestId: number, path: string) =>
    request<ProblemMatchOut>(`${ADMIN}/contests/${contestId}/problems/match`, {
      method: 'POST',
      body: { path },
    }),
}

export const agentApi = {
  list: (contestId: number) => request<AgentRuntimeOut[]>(`${ADMIN}/contests/${contestId}/agents`),
}

export const fileApi = {
  list: (
    contestId: number,
    options: { playerId?: number; includeDeleted?: boolean; limit?: number } = {},
  ) =>
    request<SourceFileOut[]>(
      `${ADMIN}/contests/${contestId}/files${query({
        player_id: options.playerId,
        include_deleted: options.includeDeleted,
        limit: options.limit,
      })}`,
    ),
}

export const eventApi = {
  list: (contestId: number, options: { limit?: number; category?: string } = {}) =>
    request<EventOut[]>(
      `${ADMIN}/contests/${contestId}/events${query({
        limit: options.limit,
        category: options.category,
      })}`,
    ),
}

export const assetApi = {
  list: (contestId: number) => request<AssetOut[]>(`${ADMIN}/contests/${contestId}/assets`),

  upload: (contestId: number, file: File, kind = 'testdata') => {
    const form = new FormData()
    form.append('file', file)
    form.append('kind', kind)
    return request<AssetOut>(`${ADMIN}/contests/${contestId}/assets`, {
      method: 'POST',
      form,
    })
  },
}

export const deployApi = {
  list: (contestId: number, includeTargets = false) =>
    request<DeployTaskOut[]>(
      `${ADMIN}/contests/${contestId}/deploys${query({ include_targets: includeTargets })}`,
    ),

  get: (taskId: number) => request<DeployTaskOut>(`${ADMIN}/deploys/${taskId}`),

  create: (contestId: number, payload: DeployCreate) =>
    request<DeployTaskOut>(`${ADMIN}/contests/${contestId}/deploys`, {
      method: 'POST',
      body: payload,
    }),

  cancel: (taskId: number) =>
    request<SimpleAck>(`${ADMIN}/deploys/${taskId}/cancel`, { method: 'POST' }),

  retry: (taskId: number) =>
    request<SimpleAck>(`${ADMIN}/deploys/${taskId}/retry`, { method: 'POST' }),
}

export const scoreApi = {
  matrix: (contestId: number) =>
    request<ScoreMatrixOut>(`${ADMIN}/contests/${contestId}/scores`),

  runs: (contestId: number, parseStatus?: string) =>
    request<JudgeRunOut[]>(
      `${ADMIN}/contests/${contestId}/judge/runs${query({ parse_status: parseStatus })}`,
    ),

  rescan: (contestId: number) =>
    request<JudgeScanOut>(`${ADMIN}/contests/${contestId}/judge/rescan`, { method: 'POST' }),

  setManual: (contestId: number, payload: ManualScoreIn) =>
    request<JudgeRunOut>(`${ADMIN}/contests/${contestId}/judge/score`, {
      method: 'PUT',
      body: payload,
    }),

  clear: (contestId: number, playerId: number, problem: string) =>
    request<SimpleAck>(
      `${ADMIN}/contests/${contestId}/judge/score${query({
        player_id: playerId,
        problem,
      })}`,
      { method: 'DELETE' },
    ),
}

export const releaseApi = {
  status: () => request<UpgradeStatusOut>(`${ADMIN}/releases`),

  upload: (file: File, version: string, notes: string, channel = 'stable') => {
    const form = new FormData()
    form.append('file', file)
    form.append('version', version)
    form.append('notes', notes)
    form.append('channel', channel)
    return request<ReleaseOut>(`${ADMIN}/releases`, { method: 'POST', form })
  },

  rollout: (releaseId: number) =>
    request<ReleaseOut>(`${ADMIN}/releases/${releaseId}/rollout`, { method: 'POST' }),

  yank: (releaseId: number) =>
    request<ReleaseOut>(`${ADMIN}/releases/${releaseId}/yank`, { method: 'POST' }),

  update: (releaseId: number, payload: ReleaseUpdate) =>
    request<ReleaseOut>(`${ADMIN}/releases/${releaseId}`, {
      method: 'PATCH',
      body: payload,
    }),
}
