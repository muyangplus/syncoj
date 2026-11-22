<script setup lang="ts">
/**
 * 添加评测机（§12.10）：输入名称 → 签发令牌 → 给出 Linux / Windows 一行安装命令与手动命令。
 *
 * 命令由服务端拼好（地址取自这次请求的 Host 头，就是教师打开管理界面用的那个），
 * 前端不自己拼：开发时页面在 Vite 的端口上，自己拼会拼出一个评测机连不上的地址。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { judgingApi } from '@/api'
import type { JudgeWorkerCreatedOut } from '@/api/types'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  created: [JudgeWorkerCreatedOut]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const name = ref('')
const submitting = ref(false)
const issued = ref<JudgeWorkerCreatedOut | null>(null)
const showManual = ref(false)

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      name.value = ''
      issued.value = null
      showManual.value = false
    }
  },
)

async function submit(): Promise<void> {
  const trimmed = name.value.trim()
  if (!trimmed || submitting.value) return
  submitting.value = true
  try {
    issued.value = await judgingApi.createWorker(trimmed)
    emit('created', issued.value)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    submitting.value = false
  }
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败')
  }
}

const commands = computed(() => {
  const item = issued.value
  if (!item) return []
  return [
    { key: 'linux', label: 'Linux', hint: 'root', text: item.linux_command },
    { key: 'windows', label: 'Windows', hint: '管理员 PowerShell', text: item.windows_command },
  ]
})
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="issued ? `添加评测机 · ${issued.name}` : '添加评测机'"
    width="min(680px, calc(100vw - 32px))"
    :close-on-click-modal="!issued"
  >
    <el-form v-if="!issued" label-position="top" @submit.prevent="submit">
      <el-form-item label="名称">
        <el-input v-model="name" maxlength="64" placeholder="如：机房 B-评测 1" autofocus />
      </el-form-item>
    </el-form>

    <template v-else>
      <el-alert type="warning" :closable="false" show-icon title="令牌仅显示这一次，关闭后无法再查看" />
      <div v-for="cmd in commands" :key="cmd.key" class="command-block">
        <div class="command-head">
          <span class="command-label">{{ cmd.label }}</span>
          <span class="command-hint">{{ cmd.hint }}</span>
          <el-button size="small" class="command-copy" @click="copy(cmd.text)">
            <el-icon><DocumentCopy /></el-icon>复制
          </el-button>
        </div>
        <pre class="command">{{ cmd.text }}</pre>
      </div>
      <el-button link type="primary" class="manual-toggle" @click="showManual = !showManual">
        手动运行<el-icon class="el-icon--right"><component :is="showManual ? 'ArrowUp' : 'ArrowDown'" /></el-icon>
      </el-button>
      <div v-if="showManual" class="command-block">
        <div class="command-head">
          <span class="command-label">令牌</span>
          <el-button size="small" class="command-copy" @click="copy(issued.token)">
            <el-icon><DocumentCopy /></el-icon>复制
          </el-button>
        </div>
        <pre class="command">{{ issued.token }}</pre>
        <div class="command-head">
          <span class="command-label">命令</span>
          <el-button size="small" class="command-copy" @click="copy(issued.manual_command)">
            <el-icon><DocumentCopy /></el-icon>复制
          </el-button>
        </div>
        <pre class="command">{{ issued.manual_command }}</pre>
      </div>
    </template>

    <template #footer>
      <template v-if="!issued">
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" :disabled="!name.trim()" @click="submit">签发令牌</el-button>
      </template>
      <el-button v-else type="primary" @click="visible = false">完成</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.command-block {
  margin-top: 14px;
}
.command-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.command-head + .command + .command-head {
  margin-top: 10px;
}
.command-label {
  font-weight: 600;
  color: var(--sj-text);
}
.command-hint {
  font-size: 12px;
  color: var(--sj-text-3);
}
.command-copy {
  margin-left: auto;
}
.command {
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--sj-radius-sm);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
  color: var(--sj-text);
  font-family: var(--sj-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.manual-toggle {
  margin-top: 12px;
}
</style>
