/** 某场次里的机器（已经配对到人的那些）。 */

import { query, request } from './client'
import { listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { AgentRuntimeOut, SimpleAck } from './types'

export const agentApi = {
  list: (contestId: number, params: ListParams = {}, signal?: AbortSignal) =>
    listPage<AgentRuntimeOut>(paths.contestAgents(contestId), { limit: 500, ...params }, signal),

  /** 改派给名单里的另一个人。机器不用重启，下一轮心跳就换身份。 */
  rebind: (agentId: number, rosterEntryId: number) =>
    request<SimpleAck>(paths.agentRebind(agentId), {
      method: 'POST',
      body: { roster_entry_id: rosterEntryId },
    }),

  /** 指定场次。`null` = 回到自动解析。 */
  setContest: (agentId: number, contestId: number | null) =>
    request<SimpleAck>(paths.agentContest(agentId), {
      method: 'POST',
      body: { contest_id: contestId },
    }),

  /** 解除绑定：机器还回来，人留着。它下一轮心跳会显示新的配对码。 */
  unbind: (agentId: number) => request<SimpleAck>(paths.agentBind(agentId), { method: 'DELETE' }),

  /**
   * 作废一台机器的凭据。
   *
   * 硬删除机器，所以 `confirm` 是主机名。作废之后那台机器下次心跳拿 401，
   * 需要重新注册并配对 —— 这是"这台机器不再属于我们"的表达。
   */
  revoke: (agentId: number, hostname: string) =>
    removeItem(paths.agent(agentId), hostname),

  /**
   * 让这台考试机把自己卸载掉（删程序、单元、注册密钥）。
   *
   * 不可逆，所以 `confirm` 同样是机器名。服务端只是**记下这次请求**：真正的
   * 授权是下一轮心跳随 `uninstall_token` 下发的签名令牌，机器验过才动手 ——
   * 所以它得先联网一次。没拿到就是没执行，重新点一次即可。
   */
  uninstall: (agentId: number, hostname: string) =>
    request<SimpleAck>(`${paths.agentUninstall(agentId)}${query({ confirm: hostname })}`, {
      method: 'POST',
    }),
}
