import { ref, watch } from 'vue'
import type { Ref } from 'vue'
import { storeToRefs } from 'pinia'

import { useContestStore } from '@/stores/contest'
import { usePolling } from '@/composables/usePolling'

export interface ContestDataOptions {
  /** 轮询间隔（毫秒） */
  interval?: number
}

/**
 * 按当前场次加载数据。
 *
 * **为什么需要它**：每个视图各自在 `onMounted` 里拉一次数据是不够的 ——
 * 组件挂载的时刻，`currentId` 往往还没从场次接口回来（它是异步的），
 * 于是首屏拉了个空；之后要等下一轮轮询（5~10 秒）才补上。
 * 刷新页面时这个现象最明显：看起来"不会自动加载数据"。
 *
 * 同一个原因还导致**切换场次后数据不刷新** —— 切了场次却还显示上一个场次的内容。
 *
 * 所以这里的核心是 `watch(currentId)`：场次一确定、或一变化，立刻重新加载。
 * 轮询只作为兜底（服务端数据被别人改动时）。
 */
export function useContestData<T>(
  loader: (contestId: number) => Promise<T>,
  options: ContestDataOptions = {},
) {
  const contest = useContestStore()
  const { currentId } = storeToRefs(contest)

  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref<string | null>(null)

  // 过期响应守卫：快速切换场次时，先发出的慢请求可能后到达并覆盖新数据。
  // 用一个自增序号标记"这一轮"，回来时对不上就丢弃。
  let generation = 0

  async function load(): Promise<void> {
    const id = currentId.value
    if (!id) {
      data.value = null
      error.value = null
      loading.value = false
      return
    }

    const mine = ++generation
    loading.value = true
    try {
      const result = await loader(id)
      if (mine !== generation) return
      data.value = result
      error.value = null
    } catch (err) {
      if (mine !== generation) return
      error.value = (err as Error).message
    } finally {
      if (mine === generation) loading.value = false
    }
  }

  // 场次确定或变化时立刻加载 —— 这是刷新页面能出数据的关键
  watch(currentId, () => void load(), { immediate: true })

  // 定时刷新作为兜底；标签页隐藏时暂停、不并发重入
  usePolling(load, { interval: options.interval ?? 8000, immediate: false })

  return { data, loading, error, reload: load }
}
