/** 某场次里的机器（已经配对到人的那些）。 */

import { request } from './client'
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
}
