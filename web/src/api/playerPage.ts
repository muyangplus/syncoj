/**
 * 考生页（**不要管理端凭据**）的接口。
 *
 * 注意名字：players.ts 里已经有一个 playerApi（管理端的“场次内选手”），
 * 两个都叫 playerApi 的话入口文件里会直接重名冲突。所以这个叫 playerPageApi。
 *
 * 三层，从谁都能看的到只有本人能看的：
 *
 * * `preview` —— 降级预览：当前在考的场次与面向全场的公告。不含任何个人信息。
 * * `context()` 不带令牌 —— 服务端按来源 IP 自动匹配本机。
 * * `login` 换令牌，`context(token)` / `download(..., token)` 带 `X-Player-Token`。
 *
 * 令牌走自定义头而不是 `Authorization`：教师在管理端的浏览器里打开这一页时，
 * `request` 会顺手带上管理员的 Bearer，两种凭据挤一个头会互相顶掉。
 * 令牌失效时服务端回 403 `player_token_invalid`（不是 401 —— 401 会被当成
 * 管理员会话过期，把教师踢回登录页）。
 *
 * 类型直接取自生成物（`./schema`）而不是先在 `types.ts` 里起别名：这一页是唯一
 * 用它的地方，多绕一层只会多一处要同步的地方。
 */

import { downloadWithServerName, request } from './client'
import { paths } from './endpoints'
import type { components } from './schema'

type S = components['schemas']

/** 台账里的一行下发文件：下发了什么、该落在哪、到没到。 */
export type PlayerAssetOut = S['PlayerAssetOut']
/** 本场的考场公告：正文取自下发给本人的那份 `NOTICE.md`，没有就是 null。 */
export type PlayerNoticeOut = S['PlayerNoticeOut']
/** 整页需要的一切，一次取回。 */
export type PlayerContextOut = S['PlayerContextOut']
/** 降级预览里的一场。 */
export type PlayerPreviewContestOut = S['PlayerPreviewContestOut']
/** 降级预览。 */
export type PlayerPreviewOut = S['PlayerPreviewOut']
export type PlayerLoginIn = S['PlayerLoginIn']
export type PlayerLoginOut = S['PlayerLoginOut']

/** 考生页令牌所在的请求头。与服务端 `api/player.py::TOKEN_HEADER` 一致。 */
export const PLAYER_TOKEN_HEADER = 'X-Player-Token'

function tokenHeaders(token?: string | null): Record<string, string> {
  return token ? { [PLAYER_TOKEN_HEADER]: token } : {}
}

export const playerPageApi = {
  /**
   * 取"这个考生"的全部内容。
   *
   * 不给令牌 = 按来源 IP 自动匹配本机；匹配不上回 403/404/409，页面退到降级预览。
   */
  context: (token?: string | null, signal?: AbortSignal) =>
    request<PlayerContextOut>(paths.playerContext(), { signal, headers: tokenHeaders(token) }),

  /** 降级预览：当前在考的场次（多场可切换）与面向全场的公告。 */
  preview: (signal?: AbortSignal) =>
    request<PlayerPreviewOut>(paths.playerPreview(), { signal }),

  /**
   * 准考证号 + 场次考生页密码 → 短期令牌。
   *
   * 失败统一 403 `player_login_failed`；场次没设密码是 409 `player_login_disabled`。
   */
  login: (payload: PlayerLoginIn) =>
    request<PlayerLoginOut>(paths.playerLogin(), { method: 'POST', body: payload }),

  /** 下载一个下发给本人的文件（令牌或本机自动匹配二选一）。 */
  download: (assetId: number, filename: string, token?: string | null) =>
    downloadWithServerName(paths.playerAsset(assetId), filename, tokenHeaders(token)),
}
