/** 镜像内置的统一注册密钥。 */

import { request } from './client'
import { listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { BootstrapKeyIssueIn, BootstrapKeyIssuedOut, BootstrapKeyOut } from './types'

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
