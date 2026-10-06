/**
 * 服务端注入的"这一份前端跑在哪个端口上"。
 *
 * 只有**公开端口**（默认 80）上才有这个对象，见 server/syncoj_server/main.py 的
 * `_public_mode_script`。管理端口上没有它，`publicMode` 因此是空对象 —— 管理界面
 * 完全不受影响。
 */
export interface SyncojPublicMode {
  /** 跑在公开端口上：只提供考生页与装机页，默认路由也压到考生页。 */
  publicOnly?: boolean
  /** API（管理）端口。装机命令要嵌它，理由见 `apiOrigin()`。 */
  adminPort?: number
}

declare global {
  interface Window {
    __SYNCOJ__?: SyncojPublicMode
  }
}

export const publicMode: SyncojPublicMode = window.__SYNCOJ__ ?? {}

/**
 * API 端口上的 origin，例如 `http://10.0.0.5:8000`。
 *
 * 装机命令里嵌的地址**取这个，不取当前页面的 origin**：它会被写进那台机器的配置、
 * 从此一直用下去。而公开端口是个"方便用的"端口（默认 80，可以关掉或换一个），
 * 管理端口才是设计上一直在的那个 —— 教师从哪个端口打开这一页，装出来的机器都该
 * 指向同一个地址。
 *
 * 主机名仍然取当前页面的：那是"从局域网真的可达"的那条真人证据，多网卡或反向
 * 代理时服务端自己算不出来（见 services/discovery.py 里那三条来源的排序）。
 */
export function apiOrigin(): string {
  const { protocol, hostname, origin } = window.location
  const port = publicMode.adminPort
  if (!port || !/^https?:$/.test(protocol)) return origin
  return `${protocol}//${hostname}:${port}`
}
