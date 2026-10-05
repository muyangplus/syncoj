import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Ref } from 'vue'

import { describeError, isAbort } from '@/api/crud'
import type { Page } from '@/api/crud'

/** 取一页的入参。`limit`/`offset` 由 useList 管理，其余由调用方自己拼。 */
export interface LoadArgs {
  limit: number
  offset: number
  signal: AbortSignal
}

export interface UseListOptions<T> {
  /**
   * 取一页。
   *
   * 返回 `null` 表示"现在还取不了"（比如还没选场次）—— 这时列表清空但**不报错**。
   * 让"没选场次"走错误通道会把一句红字挂在页面上，而那不是错误。
   */
  loader: (args: LoadArgs) => Promise<Page<T> | null>
  /**
   * 行的稳定标识。
   *
   * **必须是标识，不能是对象本身。** 每一次请求回来的都是新对象，所以用引用
   * 去比对时"还选中的行"永远比对不上 —— 那会让勾选在每一轮轮询后全部消失，
   * 而批量删除正做到一半时选中凭空消失是最不能接受的失败方式。
   */
  rowKey: (row: T) => string | number
  /**
   * 触发"回到第一页并重新加载"的来源。
   *
   * 写成 getter 数组而不是让页面自己 watch：筛选条件、场次切换都要回到第一页，
   * 而"忘记回到第一页"会让教师看到一个空表格并以为数据丢了。
   */
  watches?: (() => unknown)[]
  /** 轮询间隔（毫秒）。不传就不轮询。 */
  interval?: number
  /** 每页条数。默认 50。 */
  pageSize?: number
  /** 是否在挂载时立刻加载。默认 true。 */
  immediate?: boolean
}

/**
 * 列表数据层。
 *
 * 一个页面里"取数"该做的事只有一件：把当前的分页 + 筛选变成一个请求，
 * 然后把结果放回表格。但把它写对需要覆盖：首屏加载、筛选变化回到第一页、
 * 场次切换、轮询、取消过期请求、批量选择、以及"加载中/出错"两个状态的显示。
 * 每个页面各抄一遍的结果就是"这一页漏了第 3 条、那一页漏了第 5 条"。
 */
export function useList<T>(options: UseListOptions<T>) {
  const pageSizeRef = ref(options.pageSize ?? 50)
  /** 当次生效的每页条数。改它要走 `setPageSize`（要连带回到第一页）。 */
  const currentPageSize = () => pageSizeRef.value

  const rows = ref<T[]>([]) as Ref<T[]>
  const total = ref(0)
  const offset = ref(0)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /**
   * 选中的行。
   *
   * **按标识存**，因为每一轮请求回来的都是新对象：拿引用去比对的话，
   * "还选中的行"永远比对不上，勾选会在每一轮轮询后全部消失 ——
   * 而批量删除正做到一半时选中凭空消失是最不能接受的失败方式。
   *
   * 存在这里的是"最后一次见到的样子"（id、考号、名字都在里面），
   * 它仍在当前页时会被下面这个 computed 换成最新的一份 —— 批量操作因此
   * 永远拿的是活数据。
   */
  const selectionMap = ref(new Map<string | number, T>()) as Ref<Map<string | number, T>>

  /** 当前页的 标识 → 行，用来把选中的对象换成最新数据。 */
  const rowsByKey = computed(() => {
    const map = new Map<string | number, T>()
    for (const row of rows.value) map.set(options.rowKey(row), row)
    return map
  })

  /** 批量操作读这个：还在列表里的行用最新的，翻页翻走的用最后见到的。 */
  const selected = computed<T[]>(() => {
    const out: T[] = []
    for (const [key, row] of selectionMap.value) {
      out.push(rowsByKey.value.get(key) ?? row)
    }
    return out
  })

  const selectedKeys = computed(() => [...selectionMap.value.keys()])
  const hasSelection = computed(() => selectionMap.value.size > 0)

  let inflight: AbortController | null = null
  let timer: number | null = null

  const page = computed(() => Math.floor(offset.value / currentPageSize()) + 1)
  const pageCount = computed(() =>
    Math.max(1, Math.ceil(total.value / currentPageSize())),
  )
  /** 表格里的序号从 1 开始，跨页连续。 */
  const rowIndex = (index: number) => offset.value + index + 1

  async function load(): Promise<void> {
    // 取消上一轮：进度条不会因为一个慢请求卡住，也不会出现"旧响应覆盖新筛选"
    inflight?.abort()
    const controller = new AbortController()
    inflight = controller

    loading.value = true
    try {
      const result = await options.loader({
        limit: currentPageSize(),
        offset: offset.value,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      if (result === null) {
        rows.value = []
        total.value = 0
        error.value = null
        return
      }
      rows.value = result.items
      total.value = result.total
      // 服务端可能夹回一个不同的 limit/offset；以它为准，分页器才不会错位
      offset.value = result.offset
      error.value = null
      // 选中的行**不清**：一次后台刷新不该改变用户的勾选。el-table 配了
      // `reserve-selection`，它自己会把跨页的勾保持在新的数据上。
    } catch (err) {
      if (isAbort(err)) return
      error.value = describeError(err)
    } finally {
      if (!controller.signal.aborted) loading.value = false
      if (inflight === controller) inflight = null
    }
  }

  /** 刷新当前页。 */
  const refresh = load

  /** 回到第一页再加载。筛选条件变化时用它。 */
  async function reset(): Promise<void> {
    offset.value = 0
    clearSelection()
    await load()
  }

  function setPage(next: number): void {
    const clamped = Math.min(Math.max(1, next), pageCount.value)
    offset.value = (clamped - 1) * currentPageSize()
    void load()
  }

  function setPageSize(size: number): void {
    // 每页条数变了之后原来的 offset 落在哪一页已经没有意义，直接回第一页
    pageSizeRef.value = size
    void reset()
  }

  // 筛选条件/场次变化 → 回第一页重拉
  if (options.watches?.length) {
    watch(options.watches, () => void reset())
  }

  /**
   * 接受 el-table 的勾选结果。
   *
   * `pick` 里带的是**当前页**的存活行；但勾选状态里可能还有翻页翻走的行，
   * 而 el-table 的 `selection-change` 会把它们一起给出来（配了
   * `reserve-selection`）。所以这里不是"用 pick 覆盖"，而是"把这次给出的
   * 行按标识并进来、把这次没给出的旧选中项丢掉"—— 后者让"取消勾选"生效。
   */
  function onSelectionChange(pick: T[]): void {
    const next = new Map<string | number, T>()
    for (const row of pick) next.set(options.rowKey(row), row)
    selectionMap.value = next
  }

  function clearSelection(): void {
    selectionMap.value = new Map()
  }

  function startPolling(): void {
    if (!options.interval) return
    stopPolling()
    timer = window.setInterval(() => {
      // 标签页隐藏时暂停：教师开着管理台去干别的，后台还每 8 秒打一次接口纯属浪费
      if (document.hidden) return
      // 有勾选时不刷新：轮询会把整页数据换掉，而"勾了 10 个人准备批量删"
      // 正是最不该被后台动作打断的时刻。教师自己点刷新不受这个限制。
      if (hasSelection.value) return
      // 上一轮还没回来就跳过：慢接口会越堆越多
      if (inflight) return
      void load()
    }, options.interval)
  }

  function stopPolling(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }

  function onVisibility(): void {
    // 回到标签页补一次，但同样避开"有勾选"的时刻：勾选是用户打断不了的状态，
    // 而后台刷新是 —— 宁可他手动点一次刷新，也不要让勾选在他没看的时候消失
    if (!document.hidden && !hasSelection.value) void load()
  }

  onMounted(() => {
    if (options.immediate !== false) void load()
    startPolling()
    if (options.interval) document.addEventListener('visibilitychange', onVisibility)
  })

  onBeforeUnmount(() => {
    stopPolling()
    inflight?.abort()
    document.removeEventListener('visibilitychange', onVisibility)
  })

  return {
    rows,
    total,
    offset,
    page,
    pageCount,
    pageSize: pageSizeRef,
    loading,
    error,
    reload: refresh,
    reset,
    setPage,
    setPageSize,
    rowIndex,
    selected,
    selectedKeys,
    hasSelection,
    onSelectionChange,
    clearSelection,
  }
}
