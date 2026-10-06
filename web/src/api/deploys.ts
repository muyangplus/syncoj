/** 下发任务与逐选手进度。 */

import { request } from './client'
import { listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { DeployCreate, DeployTaskOut, SimpleAck } from './types'

export const deployApi = {
  list: (
    contestId: number,
    params: ListParams & { includeTargets?: boolean } = {},
    signal?: AbortSignal,
  ) => {
    const { includeTargets, ...rest } = params
    return listPage<DeployTaskOut>(
      paths.contestDeploys(contestId),
      { include_targets: includeTargets, ...rest },
      signal,
    )
  },

  get: (taskId: number) => request<DeployTaskOut>(paths.deploy(taskId)),

  create: (contestId: number, payload: DeployCreate) =>
    request<DeployTaskOut>(paths.contestDeploys(contestId), { method: 'POST', body: payload }),

  cancel: (taskId: number) =>
    request<SimpleAck>(paths.deployCancel(taskId), { method: 'POST' }),

  retry: (taskId: number) =>
    request<SimpleAck>(paths.deployRetry(taskId), { method: 'POST' }),

  remove: (taskId: number) => removeItem(paths.deploy(taskId)),
}
