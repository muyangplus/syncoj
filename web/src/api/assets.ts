/** 下发用的资产。 */

import { request } from './client'
import { listPage, softRemoveItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { AssetOut, AssetRenameIn } from './types'

export const assetApi = {
  list: (
    contestId: number,
    params: ListParams = {},
    signal?: AbortSignal,
  ) => listPage<AssetOut>(paths.contestAssets(contestId), { limit: 500, ...params }, signal),

  /**
   * 上传一个待下发文件。
   *
   * 服务端**原样落盘、不解压**：题面与样例都是 zip，密码由教师当成普通资产
   * 一起下发（`password.txt`）。系统不碰密码，也不需要知道它。
   */
  upload: (contestId: number, file: File, kind = 'testdata') => {
    const form = new FormData()
    form.append('file', file)
    form.append('kind', kind)
    return request<AssetOut>(paths.contestAssets(contestId), { method: 'POST', form })
  },

  /** 改名。内容按 sha256 存，改名只换标签 —— 未完成的下发任务会按新名字落地。 */
  rename: (assetId: number, payload: AssetRenameIn) =>
    request<AssetOut>(paths.asset(assetId), { method: 'PATCH', body: payload }),

  /** 软删除（墓碑）。已经落到选手机器上的文件不会撤回。 */
  remove: (assetId: number) => softRemoveItem(paths.asset(assetId)),
}
