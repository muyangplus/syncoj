import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { contestApi } from '@/api'
import type { ContestOut } from '@/api/types'

const CURRENT_KEY = 'syncoj.contest'

/**
 * 当前场次。
 *
 * 几乎所有接口都要带 contest_id，所以把"当前选的是哪个场次"收在一处，选择结果
 * 持久化到 localStorage —— 教师刷新页面不该被打回默认场次。
 */
export const useContestStore = defineStore('contest', () => {
  const contests = ref<ContestOut[]>([])
  const currentId = ref<number | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const current = computed(
    () => contests.value.find((item) => item.id === currentId.value) ?? null,
  )

  function select(id: number | null): void {
    currentId.value = id
    try {
      if (id === null) {
        window.localStorage.removeItem(CURRENT_KEY)
      } else {
        window.localStorage.setItem(CURRENT_KEY, String(id))
      }
    } catch {
      /* 隐私模式下写不了，忽略 */
    }
  }

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      contests.value = await contestApi.list()
      restoreSelection()
    } catch (err) {
      error.value = (err as Error).message
      throw err
    } finally {
      loading.value = false
    }
  }

  function restoreSelection(): void {
    if (contests.value.length === 0) {
      select(null)
      return
    }
    // 已有选择仍然有效就保留，否则退回第一个场次
    const remembered = readRemembered()
    const valid = remembered !== null && contests.value.some((c) => c.id === remembered)
    select(valid ? remembered : contests.value[0].id)
  }

  function readRemembered(): number | null {
    try {
      const raw = window.localStorage.getItem(CURRENT_KEY)
      if (!raw) return null
      const parsed = Number(raw)
      return Number.isFinite(parsed) ? parsed : null
    } catch {
      return null
    }
  }

  return { contests, currentId, current, loading, error, load, select }
})
