/**
 * 装机入口（**免登录**）。
 *
 * 这四个端点故意不鉴权：初次安装时机器手上没有 Agent、没有凭据，要求鉴权就没有
 * 入口。所以这一层不需要 token，也**不能**走管理端那套 401 处理。
 *
 * 为什么它跟"管理端接口"分开放：这里的调用者是**任意一台还没装 Agent 的机器**
 * （以及站在它前面的教师），不是登录后的管理界面。混在一起会让人以为这些接口
 * 也该有鉴权。
 */

import { request } from './client'
import { paths } from './endpoints'
import type { InstallLedgerOut } from './types'

/** 把服务端给的相对路径拼成绝对地址；已经是绝对的就不动。 */
export function absolute(base: string, path: string): string {
  const trimmed = base.replace(/\/+$/, '')
  if (/^https?:\/\//i.test(path)) return path
  return `${trimmed}${path.startsWith('/') ? '' : '/'}${path}`
}

/**
 * 空机器上的第一条命令。
 *
 * 起头必须是 curl：那台机器上除了 shell 什么都没有。地址用**当前页面的 origin**
 * —— 教师是从某个地址打开这一页的，那个地址是从局域网真的可达的地址，
 * 所以这个函数不需要服务端告诉我"我是谁"（页面上也拿不到那个信息，那接口要鉴权）。
 *
 * **刻意排成两行、中间用反斜杠续行。** 一行写下来在 720px 的卡片里会被浏览器按
 * 字符宽度软换行，而软换行的断点由容器宽度决定 —— 实测把 `--server` 拆成了
 * `--` + `server` 两行，看起来像打错了。自己给一个续行符之后断点固定、也仍然能
 * 原样粘进 shell（反斜杠 + 换行在 shell 里就是"继续"）。
 */
export function bootstrapCommand(origin: string): string {
  const base = origin.replace(/\/+$/, '')
  return (
    `curl -fsSL ${base}${paths.installBootstrap()} \\\n` +
    `  | sudo sh -s -- --server ${base}`
  )
}

export const installApi = {
  /**
   * 当前可装机版本的台账。
   *
   * **没有铺开任何版本时服务端回 404**（带一句中文说明），那是正常状态而不是
   * 错误 —— 调用方要把它显示成"请先去点铺开"，别当成故障。
   */
  ledger: (signal?: AbortSignal) =>
    request<InstallLedgerOut>(paths.installLedger(), { signal }),
}
