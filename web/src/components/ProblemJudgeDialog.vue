<script setup lang="ts">
/**
 * 一道题的评测设置（§12）：外部评测器 / 自带评测、时限内存、IO 方式、编译参数、数据包。
 */
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { judgingApi } from '@/api'
import type { ProblemOut } from '@/api/types'
import { useMutation } from '@/composables/useMutation'

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
  io_mode: 'stdio' as 'stdio' | 'file',
  compile_flags: '',
})
const current = ref<ProblemOut | null>(null)

watch(
  () => [props.modelValue, props.problem] as const,
  ([open, problem]) => {
    if (!open || !problem) return
    current.value = problem
    form.judge_mode = (problem.judge_mode as 'external' | 'builtin') || 'external'
    form.time_limit_ms = problem.time_limit_ms ?? null
    form.memory_limit_mb = problem.memory_limit_mb ?? null
    form.io_mode = (problem.io_mode as 'stdio' | 'file') || 'stdio'
    form.compile_flags = problem.compile_flags ?? ''
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
      compile_flags: form.compile_flags.trim() || null,
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
    const problem = await judgingApi.uploadTestdata(current.value.id, file)
    updated(problem)
    ElMessage.success(`数据包已上传：${problem.testdata?.cases ?? 0} 个测试点`)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    uploading.value = false
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
        <el-form-item label="时限（ms）">
          <el-input-number v-model="form.time_limit_ms" :min="100" :max="60000" :step="100" placeholder="例如 1000" />
        </el-form-item>
        <el-form-item label="内存（MB）">
          <el-input-number v-model="form.memory_limit_mb" :min="16" :max="4096" :step="16" placeholder="例如 256" />
        </el-form-item>
        <el-form-item label="输入输出">
          <el-radio-group v-model="form.io_mode">
            <el-radio value="stdio">标准输入输出</el-radio>
            <el-radio value="file">文件（{{ current?.ident }}.in / {{ current?.ident }}.out）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="编译参数">
          <el-input v-model="form.compile_flags" placeholder="例如 -std=c++14 -O2" clearable />
        </el-form-item>
        <el-form-item label="数据包">
          <div class="testdata">
            <template v-if="current?.testdata">
              <span class="mono">{{ current.testdata.name }}</span>
              <span class="cell-sub">
                {{ formatSize(current.testdata.size) }} · {{ current.testdata.cases }} 个测试点
                <template v-if="current.testdata.subtasks"> · {{ current.testdata.subtasks }} 个子任务</template>
                <template v-if="current.testdata.has_checker"> · checker</template>
                · 满分 {{ current.testdata.total_score }}
              </span>
            </template>
            <span v-else class="muted">无</span>
            <div>
              <el-button :loading="uploading" @click="fileInput?.click()">
                <el-icon><Upload /></el-icon>上传 zip
              </el-button>
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
.testdata .mono {
  color: var(--sj-text);
}
</style>
