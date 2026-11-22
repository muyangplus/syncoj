<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { TableInstance } from 'element-plus'

import { rosterApi } from '@/api'
import { describeError } from '@/api/crud'
import type { RosterMatchPairOut, RosterMatchPreviewOut } from '@/api/types'

/**
 * 名单级「按姓名匹配机器」：先预览（不写库），勾选后应用。
 *
 * 规则在服务端（services/name_match.py）：这份名单里每个未绑定的人，若恰好有一台机器
 * 在别的名单里绑着同名的人、且那台机器在这份名单里还没绑，就成一对。
 */
const props = defineProps<{ modelValue: boolean; rosterId: number | null; rosterName: string }>()
const emit = defineEmits<{ 'update:modelValue': [boolean]; applied: [] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const loading = ref(false)
const applying = ref(false)
const error = ref<string | null>(null)
const preview = ref<RosterMatchPreviewOut | null>(null)
const picked = ref<RosterMatchPairOut[]>([])
const pairTable = ref<TableInstance | null>(null)
const pairs = computed(() => preview.value?.pairs ?? [])
const conflicts = computed(() => preview.value?.conflicts ?? [])
const skipped = computed(() => preview.value?.skipped ?? [])

async function load(): Promise<void> {
  if (!props.rosterId) return
  loading.value = true
  error.value = null
  preview.value = null
  try {
    preview.value = await rosterApi.matchPreview(props.rosterId)
    picked.value = []
    // 默认全选：预览本来就只列"恰好一台"的对，教师要做的是去掉个别不对的
    await nextTick()
    for (const row of pairs.value) pairTable.value?.toggleRowSelection(row, true)
  } catch (err) {
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

watch(visible, (open) => {
  if (open) void load()
})

function onSelection(rows: RosterMatchPairOut[]): void {
  picked.value = rows
}

async function apply(): Promise<void> {
  if (!props.rosterId || !picked.value.length) return
  applying.value = true
  try {
    const result = await rosterApi.matchApply(
      props.rosterId,
      picked.value.map((pair) => ({ agent_id: pair.agent_id, roster_entry_id: pair.roster_entry_id })),
    )
    ElMessage.success(`已绑定 ${result.bound} 台`)
    for (const item of (result.failed ?? []).slice(0, 5)) {
      ElMessage.error(`${item.player_no}：${item.reason}`)
    }
    emit('applied')
    visible.value = false
  } catch (err) {
    ElMessage.error(describeError(err))
  } finally {
    applying.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="`按姓名匹配机器 · ${rosterName}`" width="760px">
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />
    <div v-loading="loading">
      <template v-if="preview">
        <h4 class="head">将要绑定 {{ pairs.length }} 对</h4>
        <el-table
          v-if="pairs.length"
          :data="pairs"
          size="small"
          max-height="300"
          ref="pairTable"
          @selection-change="onSelection"
        >
          <el-table-column type="selection" width="40" />
          <el-table-column label="考号" width="110" prop="player_no" />
          <el-table-column label="姓名" width="110">
            <template #default="{ row }">{{ row.name || '—' }}</template>
          </el-table-column>
          <el-table-column label="机器" width="150">
            <template #default="{ row }">{{ row.hostname || row.machine_id.slice(0, 16) }}</template>
          </el-table-column>
          <el-table-column label="依据" min-width="180" prop="source" />
        </el-table>

        <template v-if="conflicts.length">
          <h4 class="head">冲突 {{ conflicts.length }}</h4>
          <el-table :data="conflicts" size="small" max-height="200">
            <el-table-column label="考号" width="110" prop="player_no" />
            <el-table-column label="姓名" width="110">
              <template #default="{ row }">{{ row.name || '—' }}</template>
            </el-table-column>
            <el-table-column label="原因" min-width="200" prop="reason" />
          </el-table>
        </template>

        <template v-if="skipped.length">
          <h4 class="head">跳过 {{ skipped.length }}</h4>
          <el-table :data="skipped" size="small" max-height="200">
            <el-table-column label="考号" width="110" prop="player_no" />
            <el-table-column label="姓名" width="110">
              <template #default="{ row }">{{ row.name || '—' }}</template>
            </el-table-column>
            <el-table-column label="原因" min-width="200" prop="reason" />
          </el-table>
        </template>
      </template>
    </div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :disabled="!picked.length" :loading="applying" @click="apply">
        绑定选中的 {{ picked.length }} 对
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.head {
  margin: 12px 0 6px;
  font-size: 13px;
}
</style>
