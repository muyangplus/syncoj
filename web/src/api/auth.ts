/** 登录、注销、当前身份，以及管理端探活。 */

import { request } from './client'
import { paths } from './endpoints'
import type { AdminInfo, LoginResponse, SimpleAck } from './types'

/** `/admin/health` 的响应。字段不多，且都是概览卡片直接要用的。 */
export interface AdminHealth {
  ok: boolean
  agents_total: number
  agents_online: number
  data_root: string
}

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
