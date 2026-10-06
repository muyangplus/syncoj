/**
 * 机器配对。
 *
 * 统一密钥注册上来的机器**没有归属**（服务端还不知道它是谁），靠这里的几个
 * 接口把它认领到名单里的**人**。配对是永久的：绑的是 `roster_entry`，
 * 不是某场比赛的选手，所以同一个学生换一场比赛不用重新配。
 */

import { request } from './client'
import { clearCollection, listPage, removeItem } from './crud'
import type { ListParams } from './crud'
import { GLOBAL_CONFIRM, paths } from './endpoints'
import type { BindResultOut, CloneAlertOut, PendingMachineOut } from './types'

export const machineApi = {
  /** 待配对的机器。按最后心跳倒序 —— 教师站在机器前时它就在最上面。 */
  pending: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<PendingMachineOut>(paths.machinesPending(), { limit: 500, ...params }, signal),

  /**
   * 疑似克隆镜像：多台机器共用同一个硬件指纹。
   *
   * 必须走 `listPage` —— 服务端返回的是信封。写成 `request<CloneAlertOut[]>` 时
   * TypeScript 不会拦（那只是一个断言），运行期拿到的是**整个信封对象**，
   * 而 `v-for` 遍历对象会遍历出它的字段名：items / total / limit / offset，
   * 于是界面上冒出 4 条一模一样的告警、每条的数字都是空的。
   */
  cloneAlerts: (params: ListParams = {}, signal?: AbortSignal) =>
    listPage<CloneAlertOut>(paths.machinesCloneAlerts(), { limit: 500, ...params }, signal),

  /** 按配对码配对：机器上显示什么就输什么。六位数字，用一次即作废。 */
  bindByCode: (pairCode: string, rosterEntryId: number) =>
    request<BindResultOut>(paths.machinesBindByCode(), {
      method: 'POST',
      body: { pair_code: pairCode, roster_entry_id: rosterEntryId },
    }),

  /** 从待配对列表里点选。给了配对码就必须对得上。 */
  bind: (agentId: number, rosterEntryId: number, pairCode?: string) =>
    request<BindResultOut>(paths.machineBind(agentId), {
      method: 'POST',
      body: { roster_entry_id: rosterEntryId, pair_code: pairCode || null },
    }),

  /** 把一台待配对的机器从列表里去掉（认错机器、测试机、刷注册的垃圾）。 */
  revokePending: (agentId: number, hostname?: string) =>
    removeItem(paths.machinePending(agentId), hostname),

  /**
   * 清空待配对列表。
   *
   * 范围是"全部"，所以 `confirm` 是固定字面量 `all`（`GLOBAL_CONFIRM`）——
   * 这里没有"一个名字"可打，一次要清掉的是 N 台机器，输入**范围的名字**才是
   * 这个约定想表达的东西。
   *
   * 界面必须同时提醒**先吊销统一密钥**，否则那批机器开机还会回来。
   */
  clearPending: (confirm: string = GLOBAL_CONFIRM) =>
    clearCollection(paths.machinesPendingClear(), confirm),
}
