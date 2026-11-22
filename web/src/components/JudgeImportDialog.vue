<script setup lang="ts">
/** 导入题目评测配置：上传 → 预览（逐题改标识、选动作）→ 应用。 */
import { computed, ref, watch } from 'vue'
import type { UploadFile } from 'element-plus'
import { ElMessage } from 'element-plus'

import { JUDGE_IMPORT_FORMATS, judgeIoApi } from '@/api'
import type { JudgeImportFormat } from '@/api'
import { ApiError } from '@/api/client'
import type { JudgeImportItemOut, JudgeImportPreviewOut, ProblemOut } from '@/api/types'

const props = defineProps<{ modelValue: boolean; contestId: number | null }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'imported', problems: ProblemOut[]): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

type Action = 'create' | 'update' | 'skip'

interface Row {
  item: JudgeImportItemOut
  ident: string
  action: Action
}

const format = ref<JudgeImportFormat>('auto')
const preview = ref<JudgeImportPreviewOut | null>(null)
const rows = ref<Row[]>([])
const setBuiltin = ref(true)
const uploading = ref(false)
const applying = ref(false)
const fileName = ref('')
/** 应用时 409 的那一行 */
const conflictKey = ref<string | null>(null)

watch(visible, (open) => {
  if (!open) return
  preview.value = null
  rows.value = []
  format.value = 'auto'
  setBuiltin.value = true
  fileName.value = ''
  conflictKey.value = null
})

const formatLabel = computed(
  () => JUDGE_IMPORT_FORMATS.find((f) => f.value === preview.value?.format)?.label ?? preview.value?.format ?? '',
)
const counts = computed(() => {
  const out = { create: 0, update: 0, skip: 0 }
  for (const row of rows.value) out[row.action] += 1
  return out
})
const importing = computed(() => counts.value.create + counts.value.update)

async function pick(file: UploadFile): Promise<void> {
  if (!file.raw || !props.contestId) return
  uploading.value = true
  fileName.value = file.name
  try {
    const result = await judgeIoApi.importPreview(props.contestId, file.raw, format.value)
    preview.value = result
    rows.value = (result.problems ?? []).map((item) => ({
      item,
      ident: item.ident,
      action: (item.action ?? 'create') as Action,
    }))
    conflictKey.value = null
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    uploading.value = false
  }
}

function reset(): void {
  preview.value = null
  rows.value = []
}

async function submit(): Promise<void> {
  if (!props.contestId || !preview.value) return
  applying.value = true
  conflictKey.value = null
  try {
    const result = await judgeIoApi.importApply(props.contestId, {
      token: preview.value.token,
      items: rows.value.map((row) => ({ key: row.item.key, ident: row.ident.trim(), action: row.action })),
      set_builtin: setBuiltin.value,
    })
    ElMessage.success(`新建 ${result.created} · 覆盖 ${result.updated}`)
    emit('imported', result.problems ?? [])
    visible.value = false
  } catch (err) {
    if (err instanceof ApiError && typeof err.details?.key === 'string') {
      conflictKey.value = err.details.key
    }
    if (err instanceof ApiError && err.code === 'import_token_expired') reset()
    ElMessage.error((err as Error).message)
  } finally {
    applying.value = false
  }
}

function ioText(item: JudgeImportItemOut): string {
  if (item.io_mode !== 'file') return '标准输入输出'
  return `${item.input_file || `${item.ident}.in`} / ${item.output_file || `${item.ident}.out`}`
}

function rowClass({ row }: { row: Row }): string {
  return row.item.key === conflictKey.value ? 'row-conflict' : ''
}
</script>

<template>
  <el-dialog v-model="visible" title="导入题目配置" width="1120px" :close-on-click-modal="false">
    <template v-if="!preview">
      <el-form label-width="72px">
        <el-form-item label="格式">
          <el-select v-model="format" style="width: 200px">
            <el-option v-for="f in JUDGE_IMPORT_FORMATS" :key="f.value" :label="f.label" :value="f.value" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-upload
        v-loading="uploading"
        drag
        accept=".zip,.xml"
        :auto-upload="false"
        :show-file-list="false"
        :on-change="pick"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div>拖入或点击选择 .zip / .xml</div>
      </el-upload>
    </template>

    <template v-else>
      <div class="summary">
        <el-tag effect="plain">{{ formatLabel }}</el-tag>
        <span class="mono">{{ fileName }}</span>
        <span class="counts">
          {{ rows.length }} 道 · 新建 {{ counts.create }} · 覆盖 {{ counts.update }} · 跳过 {{ counts.skip }}
        </span>
        <el-popover v-if="preview.warnings?.length" trigger="click" width="360">
          <template #reference>
            <el-tag type="warning" class="clickable">{{ preview.warnings.length }} 条提示</el-tag>
          </template>
          <div v-for="w in preview.warnings" :key="w.code" class="notice">{{ w.message }}</div>
        </el-popover>
      </div>
      <el-table :data="rows" max-height="460" :row-class-name="rowClass">
        <el-table-column label="来源" min-width="120">
          <template #default="{ row }">
            <div class="mono">{{ row.item.source_name }}</div>
            <div v-if="row.item.title" class="cell-sub">{{ row.item.title }}</div>
          </template>
        </el-table-column>
        <el-table-column label="题目" width="150">
          <template #default="{ row }">
            <el-input v-model="row.ident" maxlength="64" :disabled="row.action === 'skip'" />
          </template>
        </el-table-column>
        <el-table-column label="动作" width="100">
          <template #default="{ row }">
            <el-select v-model="row.action">
              <el-option label="新建" value="create" :disabled="!!row.item.errors?.length" />
              <el-option label="覆盖" value="update" :disabled="!!row.item.errors?.length" />
              <el-option label="跳过" value="skip" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="时限 / 内存" width="150">
          <template #default="{ row }">
            <span class="nowrap">{{ row.item.time_limit_ms ?? 1000 }} ms / {{ row.item.memory_limit_mb ?? 256 }} MB</span>
          </template>
        </el-table-column>
        <el-table-column label="输入输出" min-width="130">
          <template #default="{ row }">
            <span class="mono">{{ ioText(row.item) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="测试点 · 子任务 · 满分" width="150">
          <template #default="{ row }">
            <template v-if="row.item.cases">
              {{ row.item.cases }} · {{ row.item.subtasks }} · {{ row.item.total_score }}
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="checker" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.item.has_checker" type="primary" effect="plain">checker</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="提示" width="160" fixed="right">
          <template #default="{ row }">
            <el-popover
              v-if="row.item.errors?.length || row.item.warnings?.length"
              trigger="click"
              width="360"
            >
              <template #reference>
                <span class="clickable">
                  <el-tag v-if="row.item.errors?.length" type="danger">{{ row.item.errors.length }} 错误</el-tag>
                  <el-tag v-if="row.item.warnings?.length" type="warning" effect="plain">
                    {{ row.item.warnings.length }} 提示
                  </el-tag>
                </span>
              </template>
              <div v-for="e in row.item.errors" :key="`e-${e.code}`" class="notice notice-error">{{ e.message }}</div>
              <div v-for="w in row.item.warnings" :key="`w-${w.code}`" class="notice">{{ w.message }}</div>
            </el-popover>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <template #footer>
      <div class="footer">
        <el-switch v-if="preview" v-model="setBuiltin" active-text="设为自带评测" />
        <span class="spacer" />
        <el-button v-if="preview" @click="reset">重新选择</el-button>
        <el-button @click="visible = false">取消</el-button>
        <el-button
          v-if="preview"
          type="primary"
          :loading="applying"
          :disabled="!importing"
          @click="submit"
        >
          导入 {{ importing }} 道
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.upload-icon {
  font-size: 40px;
  color: var(--sj-text-3);
  margin-bottom: 8px;
}
.summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.counts {
  color: var(--sj-text-2);
}
.mono {
  font-family: var(--el-font-family-mono, monospace);
}
.cell-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.muted {
  color: var(--el-text-color-secondary);
}
.nowrap {
  white-space: nowrap;
}
.clickable {
  cursor: pointer;
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
.notice {
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
}
.notice-error {
  color: var(--el-color-danger);
}
.footer {
  display: flex;
  align-items: center;
  gap: 8px;
}
.spacer {
  flex: 1;
}
:deep(.row-conflict) {
  --el-table-tr-bg-color: var(--el-color-danger-light-9);
}
</style>
