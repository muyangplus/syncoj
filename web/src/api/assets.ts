/** 下发用的资产。 */

import { request } from './client'
import { listPage, softRemoveItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type { AssetOut, AssetRenameIn, AssetTextEditIn, AssetTextOut, AssetTextSavedOut } from './types'

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

  /**
   * 读出一个**可在线编辑的纯文本资产**的正文，用来填进编辑对话框。
   *
   * 能不能改由服务端在 `AssetOut.editable` 里算好（扩展名白名单 + 体积），界面只
   * 照着它决定给不给按钮。真正打开时服务端还会再验一次字节能不能按 UTF-8 解出来 ——
   * 那一关只能读了才知道，所以列表上 `editable` 为真的文件也可能在这里拿到 400
   * （`asset_not_utf8`）。这不矛盾：列表页为了不逐行读盘，只能给一个"大概能改"。
   */
  getText: (contestId: number, assetId: number) =>
    request<AssetTextOut>(paths.assetText(contestId, assetId)),

  /**
   * 用新正文换掉同一条资产的内容。
   *
   * 它是**同一个 asset id** 换 `sha256`/`size`，不是新建一条 —— 所以引用它的下发
   * 任务与逐选手进度都不用动。已经下发成功（`done`）的机器会被服务端改回
   * `pending` 重新领一次，回执 `requeued` 就是那个台数（已经落地的文件不会自己变
   * 成新的，这个数字是"改动确实推进下去了"的唯一依据）。
   */
  saveText: (contestId: number, assetId: number, content: string) =>
    request<AssetTextSavedOut>(paths.assetText(contestId, assetId), {
      method: 'PUT',
      body: { content } satisfies AssetTextEditIn,
    }),

  /** 软删除（墓碑）。已经落到选手机器上的文件不会撤回。 */
  remove: (assetId: number) => softRemoveItem(paths.asset(assetId)),
}
