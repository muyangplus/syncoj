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
  AssetOut,
  ContestCreate,
  ContestOut,
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
  ReleaseOut,
  ReleaseUpdate,
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
