/** 代码台账、查看与导出。 */

import { download, downloadWithServerName, fetchText, query, request } from './client'
import { listPage, softRemoveItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { SimpleAck, SourceFileOut } from './types'

export const fileApi = {
  list: (
    contestId: number,
    params: ListParams & { playerId?: number; includeDeleted?: boolean } = {},
    signal?: AbortSignal,
  ) => {
    const { playerId, includeDeleted, ...rest } = params
    return listPage<SourceFileOut>(
      paths.contestFiles(contestId),
      { include_deleted: includeDeleted, player_id: playerId, ...rest },
      signal,
    )
  },

  /**
   * 导出 zip：`代码/<考号>/<路径>` 加一份带**精确字节数与 SHA256** 的清单。
   *
   * 默认 `include_deleted=true`：考场现实是"选手确实交过，只是后来被清了"，
   * 复核成绩时这份历史比干净的数据重要。
   */
  exportZip: (
    contestId: number,
    options: { playerId?: number; problem?: string; includeDeleted?: boolean } = {},
  ) =>
    downloadWithServerName(
      `${paths.contestFilesExport(contestId)}${query({
        player_id: options.playerId,
        problem: options.problem,
        include_deleted: options.includeDeleted ?? true,
      })}`,
      `代码归档-${contestId}.zip`,
    ),

  /** 取一份代码的正文。用来在界面上直接看，不落地到磁盘。 */
  content: (fileId: number) => fetchText(paths.fileContent(fileId)),

  /** 下载一份代码。服务端会按相对路径的 basename 给文件名。 */
  download: (fileId: number, fallbackName: string) =>
    download(paths.fileContent(fileId), fallbackName),

  /** 软删除（墓碑）。行还在，`include_deleted=true` 时还能看到。 */
  remove: (fileId: number) => softRemoveItem(paths.file(fileId)),

  /**
   * 清空台账。`confirm` 是**场次标识**。
   *
   * `purge=false`（默认）只清已经消失的墓碑记录 —— 不影响任何还在的东西。
   * `purge=true` 会连没消失的一起删，但机器还在报的文件下一轮就会回来。
   */
  clear: (
    contestId: number,
    slug: string,
    options: { purge?: boolean; playerId?: number } = {},
  ) =>
    request<SimpleAck>(
      // `player_id` 是查询参数（缩小范围），`confirm` 与 `purge` 是请求体
      `${paths.contestFilesClear(contestId)}${query({ player_id: options.playerId })}`,
      { method: 'POST', body: { confirm: slug, purge: options.purge ?? false } },
    ),
}
