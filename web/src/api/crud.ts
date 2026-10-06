/**
 * 分页、信封与删除语义的公共实现。
 *
 * 这一层存在的理由是"同一件事不要写第二遍"：所有列表都返回同一个信封
 * （`docs/api-conventions.md` §2），所有错误都是同一个错误体，所有**结构性
 * 数据**的删除都要带 `confirm`。页面代码因此不需要再碰 `items`/`total`、
 * 也不需要自己拼 `?confirm=`。
 *
 * 这**不是**一个通用 ORM 封装：每个资源的路径写在 `endpoints.ts` 里、方法写在
 * 各自的资源模块里（`contests.ts`、`files.ts`…），这里只放"每个资源都一样"的
 * 那部分。
 */

import { ApiError, query, request } from './client'
import { listQuery } from './endpoints'
import type { ListQuery } from './endpoints'
import type { SimpleAck } from './types'

/** 分页参数。在这里转出去，资源模块只 import `crud` 就够了。 */
export type { ListQuery }

/**
 * 列表信封。**所有**列表接口都是这个形状，包括天然很小的集合（场次、名单）——
 * 前端因此只需要一套表格与分页逻辑。
 */
export interface Page<T> {
  items: T[]
  /** 忽略 limit/offset 后的总条数。分页器与"清空 N 条"的确认文案都靠它。 */
  total: number
  limit: number
  offset: number
}

/** 查询参数值。布尔要能表达"不传"，所以允许 undefined。 */
export type Params = Record<string, string | number | boolean | undefined | null>

/**
 * 列表方法的入参：分页 + 该资源自己的筛选条件。
 *
 * 放在这里而不是 `index.ts`，是因为每个资源模块都要用它，而模块之间不该
 * 互相 import（入口只做汇总）。`index.ts` 会把它再转出去，页面照旧可以
 * `import type { ListParams } from '@/api'`。
 */
export type ListParams = ListQuery & Params

/** 一份空页。加载器用它在"还没选场次"时表达"没有数据"，而不是抛错。 */
export function emptyPage<T>(limit = 50, offset = 0): Page<T> {
  return { items: [], total: 0, limit, offset }
}

/**
 * 取一页列表。
 *
 * `signal` 一路透传到 `fetch`：切换筛选/场次时旧请求会被真正取消，而不是
 * 靠"响应回来时对一下代数"来丢弃 —— 后者在慢接口上会白占一个连接。
 */
export async function listPage<T>(
  path: string,
  params: ListQuery & Params = {},
  signal?: AbortSignal,
): Promise<Page<T>> {
  const response = await request<Partial<Page<T>>>(
    `${path}${listQuery(params as ListQuery & Record<string, unknown>)}`,
    { signal },
  )
  return normalizePage(response, params.limit ?? 50, params.offset ?? 0)
}

/**
 * 把响应整成信封。
 *
 * 服务端**约定**返回信封，但这一层多做一次兜底是有价值的：漏改的接口仍然
 * 返回裸数组时，界面会显示成空表而不是当场崩在 `undefined.items` 上 ——
 * 前者教师会来问"为什么没数据"，后者是一整页白屏外加 console 里一行栈。
 */
export function normalizePage<T>(
  body: Partial<Page<T>> | T[] | undefined | null,
  limit: number,
  offset: number,
): Page<T> {
  if (Array.isArray(body)) {
    // 裸数组：把整段当成第一页，total 就用它的长度 —— 分页器会显示成"只有这一页"
    return { items: body, total: body.length, limit, offset }
  }
  const items = Array.isArray(body?.items) ? (body.items as T[]) : []
  const total = typeof body?.total === 'number' ? body.total : items.length
  return {
    items,
    total,
    limit: typeof body?.limit === 'number' ? body.limit : limit,
    offset: typeof body?.offset === 'number' ? body.offset : offset,
  }
}

/**
 * 结构性数据的删除：`DELETE /xxx/{id}?confirm=<名称>`。
 *
 * `confirmName` 逐字等于该对象的标识（选手用 `player_no`、场次用 `slug`、
 * 名单用 `name`、机器用 `hostname`）。服务端会校验，前端弹窗只是提示 ——
 * 但这不代表前端可以省掉它：省掉的话教师唯一能看到的校验失败是提交之后
 * 那句红字。
 */
export async function removeItem(path: string, confirmName?: string): Promise<SimpleAck> {
  return request<SimpleAck>(`${path}${query({ confirm: confirmName })}`, { method: 'DELETE' })
}

/** 软删除（墓碑）。产物性数据用它 —— 行还在，`deleted_at` 被置上。 */
export async function softRemoveItem(path: string): Promise<SimpleAck> {
  return request<SimpleAck>(path, { method: 'DELETE' })
}

/**
 * 范围删除（"清空"）。
 *
 * 走 `POST .../clear` + 请求体里的 `confirm`，而不是 `DELETE ?confirm=`：
 * "清空一整个场次的选手"和"删掉某一个选手"是两种风险等级的操作，
 * 让它们在 URL 上就长得不一样，日志和权限才分得清。
 *
 * `confirm` 是**范围的名字** —— 场次范围内用 `slug`、名单范围内用 `name`、
 * 范围本身就是"全部"的（审计日志、待配对机器）用 `GLOBAL_CONFIRM`。
 */
export async function clearCollection(
  path: string,
  confirmName: string,
  extra: Record<string, unknown> = {},
): Promise<SimpleAck> {
  return request<SimpleAck>(path, {
    method: 'POST',
    body: { confirm: confirmName, ...extra },
  })
}

/**
 * 把 `ApiError` 转成一句能直接贴在提示条上的话。
 *
 * 校验失败时服务端的 `detail` 已经是"每页条数太大了（最大 500）；考号不能为空"
 * 这种揉好的中文，直接用。**不再猜** —— 老代码里那一堆
 * `typeof detail === 'string' ? ... : detail.message` 现在全在这个文件里，
 * 而且只有这一行。
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}

/** 请求被主动取消时不要把红色错误条弹出来 —— 那是用户自己切换筛选引起的。 */
export function isAbort(error: unknown): boolean {
  return (error as Error | null)?.name === 'AbortError'
}
