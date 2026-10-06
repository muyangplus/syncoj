/** Agent 发布与自更新。 */

import { request } from './client'
import { removeItem } from './crud'
import { paths } from './endpoints'
import type {
  ReleaseOut,
  ReleaseSourceOut,
  ReleaseUpdate,
  UpgradeStatusOut,
} from './types'

/**
 * 随包带下去的三条安装策略。
 *
 * 字段名与取值是**冻结的接口**（机器侧按同样的名字读），所以这里手写而不是从
 * 生成物里取 —— 服务端那三个字段目前不是独立组件，而"名字写错了"在这种集成里
 * 表现为策略静默不生效，最难查。
 */
export interface InstallPolicy {
  bootstrap_key_policy: 'keep' | 'replace'
  config_policy: Record<string, 'keep' | 'default' | 'force'>
  upgrade_mode: 'apply' | 'stage' | 'off'
}

/** `config_policy` 的三态。顺序与界面上的三选一一致。 */
export const CONFIG_POLICY_CHOICES: { value: 'keep' | 'default' | 'force'; label: string }[] = [
  { value: 'keep', label: '不改' },
  { value: 'default', label: '仅更新没人动过的' },
  { value: 'force', label: '强制覆盖' },
]

export const UPGRADE_MODE_CHOICES: {
  value: 'apply' | 'stage' | 'off'
  label: string
}[] = [
  { value: 'apply', label: 'apply（下载→验签→切换→自动重启）' },
  { value: 'stage', label: 'stage（只下载验签，不切换）' },
  { value: 'off', label: 'off（不升级）' },
]

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
   *
   * `policy` 是随包带下去的三条安装策略。字段名与机器侧读的那份逐字一致
   * （`bootstrap_key_policy` / `config_policy` / `upgrade_mode`），服务端会把它们
   * 一起放进装机台账、升级清单与包内 `install_policy.json`。`bootstrap_key_policy`
   * 留空 = 跟着"附带密钥"走（勾了就 replace）。
   */
  build: (
    version: string,
    notes = '',
    channel = 'stable',
    includeBootstrapKey = false,
    policy: Partial<InstallPolicy> = {},
  ) =>
    request<ReleaseOut>(paths.releasesBuild(), {
      method: 'POST',
      body: {
        version,
        notes: notes || null,
        channel,
        include_bootstrap_key: includeBootstrapKey,
        bootstrap_key_policy: policy.bootstrap_key_policy ?? null,
        config_policy: policy.config_policy ?? null,
        upgrade_mode: policy.upgrade_mode ?? null,
      },
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
