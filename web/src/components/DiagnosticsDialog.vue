<script setup lang="ts">
import { ref, watch } from 'vue'

import { machineApi } from '@/api'
import { describeError } from '@/api/crud'
import type { DiagnosticsOut } from '@/api/types'
import { useMutation } from '@/composables/useMutation'
import { formatBytes, formatTime } from '@/utils/format'

/**
 * 一台机器**最新一份**诊断包（Agent 定期 / 出错 / 被点名时回传的现场）。
 *
 * 每台机器只留最新一份：服务端覆盖写，所以这里没有"历史"这个概念。
 * 读了没有就点「要一份」—— 服务端点一个一次性标记，机器下一次心跳回传。
 *
 * 抽成组件是因为**两个列表都要它**：待配对的机器（`MachinesView`）与场次里的
 * 机器（`OverviewView`）。同一段对话框抄两遍，改动必然只改一处。
 */
const props = defineProps<{
  modelValue: boolean
  /** 机器在台账里的 id（`PendingMachineOut.id` / `AgentRuntimeOut.agent_id`）。 */
  agentId: number
  /** 显示给人看的机器名（主机名或 machine_id）。 */
  label: string
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
}>()

const loading = ref(false)
const error = ref<string | null>(null)
const bundle = ref<DiagnosticsOut | null>(null)

/** `reason` 显示成人话；不认识的取值原样显示（服务端只存 Agent 自报的原因）。 */
function reasonText(reason: string | null | undefined): string {
  if (reason === 'periodic') return '定期'
  if (reason === 'error') return '出错后补的'
  if (reason === 'manual') return '手动要的'
  return reason || '未说明'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    bundle.value = await machineApi.diagnostics(props.agentId)
  } catch (caught) {
    // "没收到过"是一种正常初态（机器刚装好、或刚点完「要一份」还没到下一次
    // 心跳），显示「还没有收到」而不是一条红色错误
    bundle.value = null
    if ((caught as { code?: string }).code !== 'diagnostics_not_found') {
      error.value = describeError(caught)
    }
  } finally {
    loading.value = false
  }
}

// 每次打开都重新读一次：对话框里的内容必须是"现在这一份"，不是上次打开时那份
watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    bundle.value = null
    error.value = null
    void load()
  },
)

const request = useMutation(
  () => machineApi.requestDiagnostics(props.agentId),
  // 成功提示就用服务端回执里那句（`useMutation` 默认取 `detail`）：
  // 它知道这次到底发生了什么，界面自己编一句只会把信息丢掉
  { onDone: () => { error.value = null } },
)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="诊断包"
    width="720px"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
  >
    <div v-loading="loading" style="min-height: 60px">
      <p class="cell-sub" style="margin-top: 0">
        机器：<span class="mono">{{ label || '—' }}</span>
      </p>

      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="error"
        style="margin-bottom: 12px"
      />

      <template v-if="bundle">
        <p class="cell-sub" style="margin-top: 0">
          收到时间 {{ formatTime(bundle.received_at) }} · 原因
          {{ reasonText(bundle.reason) }} · 大小 {{ formatBytes(bundle.bytes) }}
        </p>
        <pre class="diag-json mono">{{ JSON.stringify(bundle.content, null, 2) }}</pre>
      </template>
      <template v-else-if="!error">
        <p style="margin: 0">还没有收到诊断包</p>
        <p class="cell-sub">点「要一份」，机器下一次心跳会回传。</p>
      </template>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">关闭</el-button>
      <el-button v-if="bundle" :loading="loading" @click="load">刷新</el-button>
      <el-button type="primary" :loading="request.pending.value" @click="request.run(undefined)">
        要一份
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.diag-json {
  margin: 0;
  padding: 12px;
  max-height: 420px;
  overflow: auto;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  user-select: text;
}
</style>
