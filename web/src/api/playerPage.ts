/**
 * 选手页（**免登录**）的只读接口。
 *
 * 注意名字：players.ts 里已经有一个 playerApi（管理端的“场次内选手”），
 * 两个都叫 playerApi 的话入口文件里会直接重名冲突。所以这个叫 playerPageApi。
 *
 * 选手不登录，所以这一层没有 token。身份靠"场次 slug + 考号"这一对参数 ——
 * 它决定看得到哪份清单。**这一页只给三样东西**：本人所在的场次与考生信息、
 * 本场下发给他的文件清单、以及教师写的注意事项。不含代码、不含成绩、不含别人。
 *
 * 类型直接取自生成物（`./schema`）而不是先在 `types.ts` 里起别名：这一页是唯一
 * 用它的地方，多绕一层只会多一处要同步的地方。
 */

import { request } from './client'
import { listQuery, paths } from './endpoints'
import type { components } from './schema'

type S = components['schemas']

/** 台账里的一行下发文件：下发了什么、该落在哪、到没到。 */
export type PlayerAssetOut = S['PlayerAssetOut']
/** 本场的考场公告：正文取自下发给本人的那份 `NOTICE.md`，没有就是 null。 */
export type PlayerNoticeOut = S['PlayerNoticeOut']
/** 整页需要的一切，一次取回。 */
export type PlayerContextOut = S['PlayerContextOut']

export const playerPageApi = {
  /**
   * 取这一页要显示的全部内容。
   *
   * 一次请求而不是三次：这一页是给选手看的，多一次往返就多一次"半张页面"的
   * 等待，而且三份数据之间的一致（同一个场次、同一个人）本来就是服务端保证的。
   *
   * **两个参数都可选**，这是这一页的核心设计：
   *
   * * 都不给（或整个省略）→ **服务端按来源 IP 自动匹配本机**。这是正常路径：
   *   学生什么都不用填、什么都不用点。参数不能写成必填 —— 否则页面为了凑一个
   *   签名就得写空串或类型断言，而那种谎言会在"自动匹配到底发没发参数"这件事上
   *   留下疑问（断言能编过，运行时却真的带了两个空参数）。
   * * 两个都给 → 显式查。这是兜底路径：Agent 没起来，或者教师用自己的笔记本看。
   *   只给一个服务端会回 400。
   *
   * 场次或考号不存在时服务端回 404 带一句中文（"这个场次里没有编号 X 的选手"），
   * 直接显示给选手看。
   */
  context: (
    params: { contest?: string; player_no?: string } = {},
    signal?: AbortSignal,
  ) => request<PlayerContextOut>(`${paths.playerContext()}${listQuery(params)}`, { signal }),
}
