<script setup lang="ts">
/** 导出题目评测配置：选格式、选题，逐题列出导出时丢失的内容，再下载。 */
import { computed, ref, watch } from 'vue'

import { JUDGE_EXPORT_FORMATS, judgeIoApi } from '@/api'
import type { JudgeExportFormat } from '@/api'
import type { JudgeExportItemOut, ProblemOut } from '@/api/types'
import { useMutation } from '@/composables/useMutation'

const props = defineProps<{
  modelValue: boolean
  contestId: number | null
  problems: ProblemOut[]
  format: JudgeExportFormat
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const format = ref<JudgeExportFormat>('syncoj')
const selected = ref<number[]>([])
const items = ref<Record<number, JudgeExportItemOut>>({})
const previewError = ref<string | null>(null)
const loading = ref(false)

function exportable(problem: ProblemOut, fmt: JudgeExportFormat): boolean {
  return fmt === 'syncoj' || (problem.judge_mode === 'builtin' && !!problem.testdata)
}

function selectDefault(): void {
  selected.value = props.problems.filter((p) => exportable(p, format.value)).map((p) => p.id)
}

watch(visible, (open) => {
  if (!open) return
  format.value = props.format
  selectDefault()
  void refresh()
})

let serial = 0
async function refresh(): Promise<void> {
  items.value = {}
  previewError.value = null
  if (!props.contestId || !selected.value.length) return
  const mine = ++serial
  loading.value = true
  try {
    const result = await judgeIoApi.exportPreview(props.contestId, format.value, selected.value)
    if (mine !== serial) return
    items.value = Object.fromEntries((result.problems ?? []).map((item) => [item.problem_id, item]))
  } catch (err) {
    if (mine === serial) previewError.value = (err as Error).message
  } finally {
    if (mine === serial) loading.value = false
  }
}

function onFormat(): void {
  selectDefault()
  void refresh()
}

const includedCount = computed(
  () => selected.value.filter((id) => items.value[id]?.included ?? true).length,
)

const download = useMutation(
  async () => {
    if (!props.contestId) throw new Error('未选择场次')
    await judgeIoApi.exportZip(props.contestId, format.value, selected.value)
  },
  { success: '已导出', onDone: () => { visible.value = false } },
)
</script>

<template>
  <el-dialog v-model="visible" title="导出题目配置" width="640px">
    <el-form label-width="72px">
      <el-form-item label="格式">
        <el-radio-group v-model="format" @change="onFormat">
          <el-radio-button v-for="f in JUDGE_EXPORT_FORMATS" :key="f.value" :value="f.value">
            {{ f.label }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="题目">
        <el-checkbox-group v-model="selected" class="problem-list" @change="refresh">
          <div v-for="p in problems" :key="p.id" class="problem-item">
            <el-checkbox :value="p.id">
              <span class="mono">{{ p.ident }}</span>
              <span v-if="p.title" class="cell-sub">{{ p.title }}</span>
            </el-checkbox>
            <template v-if="items[p.id]">
              <div v-if="!items[p.id].included" class="notice notice-skip">已跳过</div>
              <div v-for="w in items[p.id].warnings" :key="w.code" class="notice">{{ w.message }}</div>
            </template>
          </div>
          <div v-if="!problems.length" class="muted">本场次无题目</div>
        </el-checkbox-group>
      </el-form-item>
    </el-form>
    <el-alert v-if="previewError" type="error" :title="previewError" :closable="false" show-icon />
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="download.pending.value || loading"
        :disabled="!includedCount || !!previewError"
        @click="download.run(undefined)"
      >
        <el-icon><Download /></el-icon>下载 {{ includedCount }} 道
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.problem-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  max-height: 420px;
  overflow-y: auto;
}
.problem-item {
  display: flex;
  flex-direction: column;
}
.mono {
  font-family: var(--el-font-family-mono, monospace);
}
.cell-sub {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.muted {
  color: var(--el-text-color-secondary);
}
.notice {
  margin-left: 24px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--el-color-warning);
}
.notice-skip {
  color: var(--el-text-color-secondary);
}
</style>
