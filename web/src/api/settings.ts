/**
 * 运行参数：心跳节奏与离线判定。
 *
 * 它们决定**整间机房**的节奏（机器多久心跳一次、多久算离线），所以放在「机器配对」
 * 页的工具栏里，和「装机设置」同一个位置 —— 那一页管的就是"整间机房"这类操作。
 *
 * 改完**一轮心跳内生效，不用重启服务端**：服务端每次现读这三项，下发给机器的策略
 * 每轮都带。
 */

import { request } from './client'
import { paths } from './endpoints'
import type { RuntimeSettingsOut, RuntimeSettingsUpdate } from './types'

export const settingsApi = {
  runtime: (signal?: AbortSignal) =>
    request<RuntimeSettingsOut>(paths.runtimeSettings(), { signal }),

  /**
   * 改运行参数。只传要改的项，没提到的保持当前值 —— 这样不必担心
   * "界面上的旧值把别人刚改的那一项覆盖回去"。
   */
  saveRuntime: (payload: RuntimeSettingsUpdate) =>
    request<RuntimeSettingsOut>(paths.runtimeSettings(), { method: 'PUT', body: payload }),
}
