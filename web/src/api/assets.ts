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

  /**
   * 直接在界面里写一个纯文本资产（`须知.txt`、`NOTICE.md`…）。
   *
   * 内容本来就是在浏览器里打的，为发一句话先在自己机器上造个文件是纯粹的仪式。
   * 落盘后它与上传的资产**完全一样**（同一套内容寻址 + 同一条下发流程）；服务端
   * 会把换行统一成 LF、去掉 BOM，并且**同场次 + 同名 + 同内容只留一条** ——
   * 命中已有那条时返回的是**原来那个 id**，不是新记录。
   */
  createText: (
    contestId: number,
    payload: { filename: string; content: string; kind?: string },
  ) => request<AssetOut>(paths.contestAssetsText(contestId), { method: 'POST', body: payload }),

  /** 改名。内容按 sha256 存，改名只换标签 —— 未完成的下发任务会按新名字落地。 */
  rename: (assetId: number, payload: AssetRenameIn) =>
    request<AssetOut>(paths.asset(assetId), { method: 'PATCH', body: payload }),

  /** 软删除（墓碑）。已经落到选手机器上的文件不会撤回。 */
  remove: (assetId: number) => softRemoveItem(paths.asset(assetId)),
}
