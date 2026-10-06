/**
 * 全站运行时常量（**免登录**）。
 *
 * 现在只有一样东西：显示时区。它必须是服务端下发的 —— 页面上所有时间都按它
 * 渲染，而"浏览器本地时区"与"服务端配置的时区"是两个不同的东西，让前端自己猜
 * 就等于把同一个事实放进两个真相源。
 *
 * 放在 `/api/v1/meta` 而不是管理端命名空间下：登录页、选手页、装机页都要读它，
 * 而这三个页面都拿不到管理端凭据。
 */

import { request } from './client'
import { paths } from './endpoints'
import type { MetaOut } from './types'

export const metaApi = {
  /** 全站常量的当前取值。永远是小对象，失败时调用方按默认值继续（见 main.ts）。 */
  get: (signal?: AbortSignal) => request<MetaOut>(paths.meta(), { signal }),
}
