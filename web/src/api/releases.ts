/** Agent 发布与自更新。 */

import { request } from './client'
import { removeItem } from './crud'
import { paths } from './endpoints'
import type { ReleaseOut, ReleaseSourceOut, ReleaseUpdate, UpgradeStatusOut } from './types'

export const releaseApi = {
  status: () => request<UpgradeStatusOut>(paths.releases()),

  /**
   * 本机有没有可构建的源码、会打出哪个版本。
   *
   * **永远 200**，不能用它来判"出没出错"：没有源码是正常状态
   * （生产上服务端可能根本没 checkout），要显示成一句解释。
   */
  source: () => request<ReleaseSourceOut>(paths.releasesSource()),

  /**
   * 从本机仓库的 `agent/` 源码构建并签发一个版本。
   *
   * `version` 必填 —— 界面会预填 `source()` 给的那个值，但服务端不接受默认值：
   * 发版必须经过一次显式确认。构建出来的是**草稿**，还要再点铺开才会推给机器。
   *
   * `includeBootstrapKey` 打开时，服务端会在**构建那一刻现场签发**一把统一注册
   * 密钥、塞进包里，并把它的 id 记在版本记录上（默认关）。服务端只有密钥的哈希、
   * 拿不到明文，所以带不动"已经签发过的那一把" —— 每次带 key 发版都是一把独立的
   * 密钥，也就能按版本单独吊销。
   */
  build: (version: string, notes = '', channel = 'stable', includeBootstrapKey = false) =>
    request<ReleaseOut>(paths.releasesBuild(), {
      method: 'POST',
      body: { version, notes: notes || null, channel, include_bootstrap_key: includeBootstrapKey },
    }),

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
