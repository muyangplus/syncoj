<script setup lang="ts">
/**
 * 一道题的评测设置（§12）：外部评测器 / 自带评测、题型（§12.13）、时限内存、IO 方式与文件名、编译参数、
 * 比较方式（§12.16）、数据包。
 */
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { JUDGE_EXPORT_FORMATS, judgeIoApi, judgingApi } from '@/api'
import type { JudgeExportFormat } from '@/api'
import { ApiError } from '@/api/client'
import type { ProblemOut } from '@/api/types'
import { useMutation } from '@/composables/useMutation'
import { COMPARE_MODES, PROBLEM_TYPES, problemTypeLabel } from '@/utils/judgeStatus'
import type { CompareMode, ProblemType } from '@/utils/judgeStatus'

const props = defineProps<{ modelValue: boolean; problem: ProblemOut | null }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'saved', problem: ProblemOut): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const form = reactive({
  judge_mode: 'external' as 'external' | 'builtin',
  time_limit_ms: null as number | null,
  memory_limit_mb: null as number | null,
  io_mode: 'stdio' as 'stdio' | 'file' | 'auto',
  input_file: '',
  output_file: '',
  compile_flags: '',
  problem_type: 'traditional' as ProblemType,
  compare_mode: 'both' as CompareMode,
})
const isAnswerOnly = computed(() => form.problem_type === 'answer_only')
const isInteractive = computed(() => form.problem_type === 'interactive')
const usesCompare = computed(() => !isInteractive.value && !current.value?.testdata?.has_checker)

watch(
  () => form.problem_type,
  (kind) => {
    if (kind === 'interactive') form.io_mode = 'stdio'
  },
)
const current = ref<ProblemOut | null>(null)

watch(
  () => [props.modelValue, props.problem] as const,
  ([open, problem]) => {
    if (!open || !problem) return
    current.value = problem
    form.judge_mode = (problem.judge_mode as 'external' | 'builtin') || 'external'
    form.time_limit_ms = problem.time_limit_ms ?? null
    form.memory_limit_mb = problem.memory_limit_mb ?? null
    form.io_mode = (problem.io_mode as 'stdio' | 'file' | 'auto') || 'stdio'
    form.input_file = problem.input_file ?? ''
    form.output_file = problem.output_file ?? ''
    form.compile_flags = problem.compile_flags ?? ''
    form.problem_type = (problem.problem_type as ProblemType) || 'traditional'
    form.compare_mode = (problem.compare_mode as CompareMode) || 'both'
  },
  { immediate: true },
)

function updated(problem: ProblemOut): void {
  current.value = problem
  emit('saved', problem)
}

const save = useMutation(
  () =>
    judgingApi.setProblemJudge(current.value!.id, {
      judge_mode: form.judge_mode,
      time_limit_ms: form.time_limit_ms || null,
      memory_limit_mb: form.memory_limit_mb || null,
      io_mode: form.io_mode,
      input_file: (form.io_mode !== 'stdio' && form.input_file.trim()) || null,
      output_file: (form.io_mode !== 'stdio' && form.output_file.trim()) || null,
      compile_flags: form.compile_flags.trim() || null,
      problem_type: form.problem_type,
      compare_mode: form.compare_mode,
    }),
  {
    success: '已保存',
    onDone: (problem) => {
      updated(problem)
      visible.value = false
    },
  },
)

const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !current.value) return
  uploading.value = true
  try {
    await upload(file)
  } finally {
    uploading.value = false
  }
}

async function upload(file: File, problemType?: string): Promise<void> {
  if (!current.value) return
  try {
    const problem = await judgingApi.uploadTestdata(current.value.id, file, problemType)
    updated(problem)
    form.problem_type = (problem.problem_type as ProblemType) || 'traditional'
    ElMessage.success(`数据包已上传：${problem.testdata?.cases ?? 0} 个测试点`)
  } catch (err) {
    const declared = err instanceof ApiError && err.code === 'testdata_type_mismatch'
      ? String(err.details?.declared ?? '')
      : ''
    if (!declared) {
      ElMessage.error((err as Error).message)
      return
    }
    // 数据包写的题型与题目不一致（§12.13）：确认后改题型并上传
    try {
      await ElMessageBox.confirm(
        `数据包是${problemTypeLabel(declared)}题，题目是${problemTypeLabel(form.problem_type)}题。`,
        '题型不一致',
        { type: 'warning', confirmButtonText: `改为${problemTypeLabel(declared)}并上传`, cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    await upload(file, declared)
  }
}

async function removeTestdata(): Promise<void> {
  if (!current.value?.testdata) return
  try {
    await ElMessageBox.confirm(`将删除 ${current.value.testdata.name}，不可恢复。`, '删除数据包？', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    updated(await judgingApi.removeTestdata(current.value.id))
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function downloadTestdata(): Promise<void> {
  if (!current.value?.testdata) return
  try {
    await judgeIoApi.downloadTestdata(current.value.id, current.value.testdata.name || 'testdata.zip')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function exportProblem(format: JudgeExportFormat): Promise<void> {
  if (!current.value) return
  try {
    await judgeIoApi.exportProblem(current.value.id, format)
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

function formatSize(bytes: number): string {
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="`评测设置 · ${current?.ident ?? ''}`"
    width="560px"
    :close-on-click-modal="false"
  >
    <el-form label-width="96px">
      <el-form-item label="评测方式">
        <el-radio-group v-model="form.judge_mode">
          <el-radio-button value="external">外部评测器</el-radio-button>
          <el-radio-button value="builtin">自带评测</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <template v-if="form.judge_mode === 'builtin'">
        <el-form-item label="题型">
          <el-radio-group v-model="form.problem_type">
            <el-radio-button v-for="t in PROBLEM_TYPES" :key="t.value" :value="t.value">{{ t.label }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="!isAnswerOnly">
        <el-form-item label="时限（ms）">
          <el-input-number v-model="form.time_limit_ms" :min="100" :max="60000" :step="100" placeholder="例如 1000" />
        </el-form-item>
        <el-form-item label="内存（MB）">
          <el-input-number v-model="form.memory_limit_mb" :min="16" :max="4096" :step="16" placeholder="例如 256" />
        </el-form-item>
        <el-form-item label="输入输出">
          <el-radio-group v-model="form.io_mode">
            <el-radio value="stdio">标准输入输出</el-radio>
            <el-radio value="file" :disabled="isInteractive">文件（freopen）</el-radio>
            <el-radio value="auto" :disabled="isInteractive">自动</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.io_mode !== 'stdio'">
          <el-form-item label="输入文件">
            <el-input v-model="form.input_file" :placeholder="`${current?.ident ?? ''}.in`" maxlength="64" clearable />
          </el-form-item>
          <el-form-item label="输出文件">
            <el-input v-model="form.output_file" :placeholder="`${current?.ident ?? ''}.out`" maxlength="64" clearable />
          </el-form-item>
        </template>
        <el-form-item label="编译参数">
          <el-input v-model="form.compile_flags" placeholder="例如 -std=c++14 -O2" clearable />
        </el-form-item>
        </template>
        <el-form-item v-if="usesCompare" label="比较方式">
          <el-radio-group v-model="form.compare_mode">
            <el-radio v-for="m in COMPARE_MODES" :key="m.value" :value="m.value">{{ m.label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="数据包">
          <div class="testdata">
            <template v-if="current?.testdata">
              <span class="mono">{{ current.testdata.name }}</span>
              <span class="cell-sub">
                {{ formatSize(current.testdata.size) }} · {{ current.testdata.cases }} 个测试点
                <template v-if="current.testdata.subtasks"> · {{ current.testdata.subtasks }} 个子任务</template>
                <template v-if="current.testdata.has_checker">
                  · checker {{ current.testdata.checker_protocol }}<template
                    v-if="current.testdata.checker_protocol === 'testlib'"
                  > / {{ current.testdata.checker_points }}</template>
                </template>
                <template v-if="current.testdata.has_interactor"> · interactor</template>
                <template v-if="current.testdata.grader_files?.length">
                  · grader + {{ current.testdata.grader_files.join('、') }}
                </template>
                <template v-if="current.testdata.answer_count">
                  · 答案文件 {{ current.testdata.answer_count }} 个（{{ (current.testdata.answer_exts ?? []).join(' ') }}）
                </template>
                · 满分 {{ current.testdata.total_score }}
              </span>
              <div v-if="current.testdata.warnings?.length" class="testdata-warnings">
                <el-tooltip v-for="w in current.testdata.warnings" :key="w.code + w.message" :content="w.message">
                  <el-tag size="small" type="warning" effect="plain">{{ w.code }}</el-tag>
                </el-tooltip>
              </div>
            </template>
            <span v-else class="muted">无</span>
            <div class="testdata-actions">
              <el-button :loading="uploading" @click="fileInput?.click()">
                <el-icon><Upload /></el-icon>上传 zip
              </el-button>
              <el-button v-if="current?.testdata" @click="downloadTestdata">
                <el-icon><Download /></el-icon>下载
              </el-button>
              <el-dropdown trigger="click" @command="exportProblem">
                <el-button>
                  导出<el-icon class="el-icon--right"><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item
                      v-for="f in JUDGE_EXPORT_FORMATS"
                      :key="f.value"
                      :command="f.value"
                      :disabled="f.value !== 'syncoj' && !current?.testdata"
                    >
                      {{ f.label }}
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-button v-if="current?.testdata" type="danger" plain @click="removeTestdata">
                删除
              </el-button>
            </div>
            <input ref="fileInput" type="file" accept=".zip" hidden @change="onPick" />
          </div>
        </el-form-item>
      </template>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="save.pending.value" @click="save.run(undefined)">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.testdata {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.testdata-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.testdata-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}
.testdata-warnings {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.testdata .mono {
  color: var(--sj-text);
}
</style>
