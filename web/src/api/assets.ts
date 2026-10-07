/** 下发用的资产。 */

import { request } from './client'
import { listPage, softRemoveItem } from './crud'
import type { ListParams } from './crud'
import { paths } from './endpoints'
import type {
  AssetOut,
  AssetRenameIn,
  AssetTextEditIn,
  AssetTextOut,
  AssetTextSavedOut,
  AssetZipPasswordIn,
  AssetZipPasswordOut,
  AssetZipPasswordSavedOut,
} from './types'

export const assetApi = {
  list: (
    contestId: number,
    params: ListParams = {},
    signal?: AbortSignal,
  ) => listPage<AssetOut>(paths.contestAssets(contestId), { limit: 500, ...params }, signal),

  /**
   * 上传一个待下发文件。
   *
   * 默认**原样落盘、不解压**：题面与样例本来就是 zip，密码由教师当成普通资产
   * 一起下发（`password.txt`）。
   *
   * `packaging.packageZip=true` 时反过来：服务端直接把字节包成 zip 再落盘
   * （成员名 = 原文件名，资产名 = `<原基名>.zip`），并写/更新一份
   * `password.txt`。`zipPassword` 留空 = 服务端生成一个随机密码；上传回执是
   * `AssetOut`，里面**没有**密码字段 —— 想抄下来只能去读那份 `password.txt`。
   */
  upload: (
    contestId: number,
    file: File,
    kind = 'testdata',
    packaging: { packageZip?: boolean; zipPassword?: string } = {},
  ) => {
    const form = new FormData()
    form.append('file', file)
    form.append('kind', kind)
    // 不打包就不带这两个字段：服务端的默认值就是"不打包"。
    if (packaging.packageZip) {
      form.append('package_zip', 'true')
      if (packaging.zipPassword) form.append('zip_password', packaging.zipPassword)
    }
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

  /**
   * 这个 zip 现在有没有密码。
   *
   * 只读 zip 的标志位（服务端不解压、不解密任何成员）。**不是 zip 的文件会拿到
   * 400 `asset_not_zip`** —— 那不是失败，而是"该走打包"的信号（见
   * `setZipPassword`）。界面应当在打开对话框之前先问一次，而不是拿扩展名猜：
   * 一个叫 `.pdf` 的文件可能真是个包，一个叫 `.zip` 的文件也可能不是。
   */
  getZipPassword: (contestId: number, assetId: number) =>
    request<AssetZipPasswordOut>(paths.assetZipPassword(contestId, assetId)),

  /**
   * 给 zip 打密码 / 改密码；**资产本来不是 zip 时，先把它打包成 zip**。
   *
   * 已经加密的包必须给 `old_password`（旧密码就在之前那份 password.txt 里）；
   * `generate=true` 时服务端用 `secrets` 生成一个不含易混字符的随机密码。
   * 回执里的 `password` 是**唯一一次**回显 —— 之后想再查只能读
   * `password_asset` 那份 password.txt（服务端不另存密码）。
   *
   * `packaged` 区分这次是"打包"还是"改密码"：只有打包会把
   * `asset.filename` 换成 `<原基名>.zip`（zip 里的成员名仍是原文件名），
   * 回执与界面提示都要跟着它说，否则教师会以为文件"自己改名了"。
   *
   * **这是弱加密**：挡得住随手翻看，挡不住有心人。别把它当成保护机密数据的手段。
   */
  setZipPassword: (contestId: number, assetId: number, payload: AssetZipPasswordIn) =>
    request<AssetZipPasswordSavedOut>(paths.assetZipPassword(contestId, assetId), {
      method: 'POST',
      body: payload,
    }),
}
