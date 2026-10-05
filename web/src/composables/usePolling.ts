import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Ref } from 'vue'

export interface PollingOptions {
  /** 轮询间隔（毫秒） */
  interval?: number
  /** 是否立即执行一次 */
  immediate?: boolean
}

/**
 * 周期刷新。
 *
 * 两个细节值得说明：
 *
 * 1. **标签页隐藏时暂停**。教师开着管理台去干别的，后台还每 5 秒打一次接口纯属
 *    浪费 —— 而"50 台机器 + 一个开着十个标签页的教师"并不夸张。重新可见时立刻
 *    补一次，不会看到过期数据。
 * 2. **不并发重入**。上一轮还没返回就跳过这一轮，否则慢接口会越堆越多。
 */
export function usePolling(
  task: () => Promise<void> | void,
  options: PollingOptions = {},
): { refresh: () => Promise<void>; active: Ref<boolean> } {
  const interval = options.interval ?? 5000
  const active = ref(true)
  let timer: number | null = null
  let running = false

  async function refresh(): Promise<void> {
    if (running) return
    running = true
    try {
      await task()
    } finally {
      running = false
    }
  }

  function stop(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }

  function start(): void {
    stop()
    timer = window.setInterval(() => {
      if (document.hidden) return
      void refresh()
    }, interval)
  }

  function onVisibility(): void {
    if (!document.hidden) void refresh()
  }

  onMounted(() => {
    if (options.immediate !== false) void refresh()
    start()
    document.addEventListener('visibilitychange', onVisibility)
  })

  onBeforeUnmount(() => {
    stop()
    document.removeEventListener('visibilitychange', onVisibility)
  })

  // 暂停开关：需要"手动操作期间不打接口"时可以切掉
  watch(active, (value) => (value ? start() : stop()))

  return { refresh, active }
}
