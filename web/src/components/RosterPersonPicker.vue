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
const props = withDefaults(
  defineProps<{
    /** 选中的名单条目 id。`null` 表示还没选。 */
    modelValue: number | null
    defaultRosterId?: number | null
    /** 用不上的地方可以隐藏"名单"那一格（比如名单已经由外部定好）。 */
    showRoster?: boolean
  }>(),
  // 布尔 prop 未传时 Vue 会给 false，不写默认值名单下拉就永远被藏掉
  { defaultRosterId: null, showRoster: true },
)

const emit = defineEmits<{ 'update:modelValue': [number | null] }>()

const rosters = ref<RosterOut[]>([])
const rostersLoaded = ref(false)
const rosterId = ref<number | null>(null)
/** 教师是否亲手选过名单；选过之后不再被默认名单覆盖。 */
const pickedByUser = ref(false)
const entries = ref<RosterEntryOut[]>([])
const loading = ref(false)
/** 条目请求序号：快速换名单时，晚回来的旧请求直接丢弃。 */
let entriesSeq = 0

async function loadRosters(): Promise<void> {
  try {
    const page = await rosterApi.list()
    rosters.value = page.items
    rostersLoaded.value = true
  } catch (err) {
    ElMessage.error(`名单库加载失败：${(err as Error).message}`)
  }
}

function exists(id: number | null): boolean {
  return id !== null && (!rostersLoaded.value || rosters.value.some((item) => item.id === id))
}

/** 该显示哪份名单：手选且仍存在的优先，其次场次默认名单，再次第一份。 */
function resolveRosterId(): number | null {
  if (pickedByUser.value && exists(rosterId.value)) return rosterId.value
  pickedByUser.value = false
  if (exists(props.defaultRosterId)) return props.defaultRosterId
  return rosters.value[0]?.id ?? null
}

async function loadEntries(): Promise<void> {
  // 换名单必须把已选的人清掉：名单 A 的第 5 条和名单 B 的第 5 条是两个人，
  // 留着旧 id 会绑到一个完全无关的人身上，而且不报错
  const seq = ++entriesSeq
  const id = rosterId.value
  emit('update:modelValue', null)
  entries.value = []
  if (!id) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const detail = await rosterApi.get(id)
    if (seq !== entriesSeq || id !== rosterId.value) return
    entries.value = detail.entries ?? []
  } catch (err) {
    if (seq !== entriesSeq) return
    ElMessage.error(`名单条目加载失败：${(err as Error).message}`)
  } finally {
    if (seq === entriesSeq) loading.value = false
  }
}

/** 设定当前名单；没变化时也重拉条目（reload 需要拿到最新的人）。 */
function applyRosterId(next: number | null, forceEntries: boolean): void {
  if (next !== rosterId.value) rosterId.value = next
  else if (forceEntries) void loadEntries()
}

function onPickRoster(value: number): void {
  pickedByUser.value = true
  rosterId.value = value
}

/**
 * 重拉名单列表与当前名单的条目。组件常驻在弹窗里只挂载一次，
 * 调用方在每次打开弹窗时调它，才能看到新建的名单与最新条目。
 */
async function reload(): Promise<void> {
  await loadRosters()
  applyRosterId(resolveRosterId(), true)
}

watch(rosterId, () => void loadEntries())
// 换场次后默认名单变了：教师没手选过就跟着走
watch(
  () => props.defaultRosterId,
  () => {
    if (!pickedByUser.value) applyRosterId(resolveRosterId(), false)
  },
)
void reload()

const rosterName = computed(
  () => rosters.value.find((item) => item.id === rosterId.value)?.name ?? '',
)

/** 供调用方在提示文案里说清"这个人在哪份名单上"。 */
defineExpose({ entries, rosterName, reload })
</script>

<template>
  <div class="picker">
    <el-select
      v-if="showRoster"
      :model-value="rosterId ?? undefined"
      placeholder="选择名单"
      style="width: 180px"
      @update:model-value="onPickRoster"
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
      placeholder="选择人员"
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
