import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { contestApi } from '@/api'
import type { ContestOut } from '@/api/types'

const CURRENT_KEY = 'syncoj.contest'
const SHOW_ARCHIVED_KEY = 'syncoj.contest.showArchived'

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

  /** 顶栏下拉要不要列出已归档的场次（默认不列）。 */
  const showArchived = ref(readFlag(SHOW_ARCHIVED_KEY))

  /** 未归档的场次。场次管理页与默认的下拉只看这些。 */
  const active = computed(() => contests.value.filter((item) => item.status !== 'archived'))
  const archived = computed(() => contests.value.filter((item) => item.status === 'archived'))

  /**
   * 顶栏下拉的选项：未归档的，加上（开关打开时）已归档的。
   * 当前选中的是已归档场次时也列出它 —— 否则下拉框里显示的是一个裸 id。
   */
  const options = computed(() =>
    contests.value.filter(
      (item) => item.status !== 'archived' || showArchived.value || item.id === currentId.value,
    ),
  )

  function setShowArchived(value: boolean): void {
    showArchived.value = value
    try {
      window.localStorage.setItem(SHOW_ARCHIVED_KEY, value ? '1' : '0')
    } catch {
      /* 隐私模式下写不了，忽略 */
    }
  }

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
      // 列表接口统一走分页信封（见 docs/reference/api-conventions.md §2），场次也不例外。
      // 场次是天然很小的集合，取法是一页装下全部 —— 这里只关心 items。
      const page = await contestApi.list()
      contests.value = page.items
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
    // 已有选择仍然有效就保留，否则退回第一个未归档的场次
    const remembered = readRemembered()
    const valid = remembered !== null && contests.value.some((c) => c.id === remembered)
    select(valid ? remembered : (active.value[0] ?? contests.value[0]).id)
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

  function readFlag(key: string): boolean {
    try {
      return window.localStorage.getItem(key) === '1'
    } catch {
      return false
    }
  }

  return {
    contests,
    active,
    archived,
    options,
    showArchived,
    setShowArchived,
    currentId,
    current,
    loading,
    error,
    load,
    select,
  }
})
