<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { rosterApi } from '@/api'
import type { RosterEntryOut, RosterOut } from '@/api/types'

/**
 * 选一个"人"。
 *
 * 机器配对与改派都要回答同一个问题：**绑给名单里的谁。** 绑的对象是名单条目
 * （一个人），不是某场比赛的选手 —— 所以必须两步：先选名单，再选人。
 * 两处各写一遍的结果一定是其中一处忘了"换名单要清掉已选的人"。
 *
 * `defaultRosterId` 通常传当前场次的默认名单：那几乎总是教师想绑的那份。
 */
const props = defineProps<{
  /** 选中的名单条目 id。`null` 表示还没选。 */
  modelValue: number | null
  defaultRosterId?: number | null
  /** 用不上的地方可以隐藏"名单"那一格（比如名单已经由外部定好）。 */
  showRoster?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [number | null] }>()

const rosters = ref<RosterOut[]>([])
const rosterId = ref<number | null>(props.defaultRosterId ?? null)
const entries = ref<RosterEntryOut[]>([])
const loading = ref(false)

async function loadRosters(): Promise<void> {
  try {
    const page = await rosterApi.list()
    rosters.value = page.items
    // 默认名单只在第一次决定；之后以教师自己选的为准
    if (rosterId.value === null) {
      rosterId.value = props.defaultRosterId ?? rosters.value[0]?.id ?? null
    }
  } catch (err) {
    ElMessage.error(`名单库加载失败：${(err as Error).message}`)
  }
}

async function loadEntries(): Promise<void> {
  // 换名单必须把已选的人清掉：名单 A 的第 5 条和名单 B 的第 5 条是两个人，
  // 留着旧 id 会绑到一个完全无关的人身上，而且不报错
  emit('update:modelValue', null)
  entries.value = []
  if (!rosterId.value) return
  loading.value = true
  try {
    const detail = await rosterApi.get(rosterId.value)
    entries.value = detail.entries ?? []
  } catch (err) {
    ElMessage.error(`名单条目加载失败：${(err as Error).message}`)
  } finally {
    loading.value = false
  }
}

watch(rosterId, () => void loadEntries())
// 默认名单是异步来的，到了之后再触发一次加载
watch(
  () => props.defaultRosterId,
  (value) => {
    if (rosterId.value === null && value) rosterId.value = value
  },
)
void loadRosters().then(() => {
  if (rosterId.value !== null) void loadEntries()
})

const rosterName = computed(
  () => rosters.value.find((item) => item.id === rosterId.value)?.name ?? '',
)

/** 供调用方在提示文案里说清"这个人在哪份名单上"。 */
defineExpose({ entries, rosterName })
</script>

<template>
  <div class="picker">
    <el-select
      v-if="showRoster !== false"
      v-model="rosterId"
      placeholder="选择名单"
      style="width: 180px"
    >
      <el-option
        v-for="roster in rosters"
        :key="roster.id"
        :label="`${roster.name}（${roster.entry_count} 人）`"
        :value="roster.id"
      />
    </el-select>

    <el-select
      :model-value="modelValue ?? undefined"
      placeholder="选择这个人"
      filterable
      :loading="loading"
      :disabled="!rosterId"
      style="width: 240px"
      @update:model-value="(value: number) => emit('update:modelValue', value)"
    >
      <el-option
        v-for="entry in entries"
        :key="entry.id"
        :label="`${entry.player_no}${entry.name ? ' ' + entry.name : ''}${
          entry.seat ? ` · 座位 ${entry.seat}` : ''
        }`"
        :value="entry.id"
      />
    </el-select>
  </div>
</template>

<style scoped>
.picker {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
