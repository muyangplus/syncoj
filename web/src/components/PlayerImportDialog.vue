<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { playerApi } from '@/api'
import type { PlayerOut, PlayerUpsert } from '@/api/types'

const props = defineProps<{
  modelValue: boolean
  contestId: number | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  imported: [players: PlayerOut[]]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const submitting = ref(false)
const raw = ref('')

interface ParsedRow {
  line: number
  player_no: string
  name: string
  seat: string
  group_name: string
  error?: string
}

/**
 * 按行解析。
 *
 * 列分隔符依次尝试 **制表符 → 英文逗号 → 中文逗号 → 连续空白**：
 * 教师多半是从 Excel 粘过来的（制表符），也可能是手敲的（空格或逗号）。
 * 直接按空白切会把"张 三"这种带空格的名字切坏，所以逗号优先于空白。
 */
function splitColumns(line: string): string[] {
  if (line.includes('\t')) return line.split('\t')
  if (line.includes(',')) return line.split(',')
  if (line.includes('，')) return line.split('，')
  return line.split(/\s+/)
}

const parsed = computed<ParsedRow[]>(() => {
  const seen = new Set<string>()
  const rows: ParsedRow[] = []

  raw.value.split(/\r?\n/).forEach((line, index) => {
    const text = line.trim()
    if (!text || text.startsWith('#')) return // 允许用 # 写注释

    const columns = splitColumns(text).map((cell) => cell.trim())
    const [playerNo = '', name = '', seat = '', group = ''] = columns

    const row: ParsedRow = {
      line: index + 1,
      player_no: playerNo,
      name,
      seat,
      group_name: group,
    }

    if (!playerNo) {
      row.error = '缺少选手编号'
    } else if (playerNo.length > 64) {
      row.error = '编号超过 64 字符'
    } else if (seen.has(playerNo)) {
      row.error = '本次粘贴中重复'
    } else {
      seen.add(playerNo)
    }
    rows.push(row)
  })

  return rows
})

const validRows = computed(() => parsed.value.filter((row) => !row.error))
const invalidRows = computed(() => parsed.value.filter((row) => row.error))

watch(visible, (open) => {
  if (open) raw.value = ''
})

async function submit(): Promise<void> {
  if (!props.contestId) {
    ElMessage.warning('请先选择场次')
    return
  }
  if (!validRows.value.length) {
    ElMessage.warning('没有可导入的选手')
    return
  }

  submitting.value = true
  try {
    const payload: PlayerUpsert[] = validRows.value.map((row) => ({
      player_no: row.player_no,
      name: row.name || null,
      seat: row.seat || null,
      group_name: row.group_name || null,
    }))
    const players = await playerApi.import(props.contestId, payload)
    ElMessage.success(`已导入 ${players.length} 名选手`)
    visible.value = false
    emit('imported', players)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="导入选手" width="720px" :close-on-click-modal="false">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>每行一名选手，用逗号、制表符或空格分隔</template>
      <template #default>
        <code>编号,姓名,座位,分组</code> —— 只有编号是必填的。
        直接从 Excel 复制粘贴即可（制表符分隔）。以 <code>#</code> 开头的行会被忽略。
        <br />
        <strong>相同编号会更新已有选手</strong>，所以这份名单可以反复导入。
      </template>
    </el-alert>

    <el-input
      v-model="raw"
      type="textarea"
      :rows="10"
      placeholder="S001,张三,A1,A组&#10;S002,李四,A2,A组&#10;S003,王五,B1,B组"
    />

    <div v-if="parsed.length" class="preview">
      <div class="preview-head">
        <span>解析预览</span>
        <span>
          <el-tag type="success" size="small" effect="plain">
            {{ validRows.length }} 条可导入
          </el-tag>
          <el-tag
            v-if="invalidRows.length"
            type="danger"
            size="small"
            effect="plain"
            style="margin-left: 6px"
          >
            {{ invalidRows.length }} 条有问题
          </el-tag>
        </span>
      </div>

      <el-table :data="parsed" size="small" border max-height="220">
        <el-table-column label="行" prop="line" width="55" align="right" />
        <el-table-column label="编号" prop="player_no" width="110">
          <template #default="{ row }">
            <span class="mono">{{ row.player_no || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="姓名" prop="name" width="110">
          <template #default="{ row }">{{ row.name || '—' }}</template>
        </el-table-column>
        <el-table-column label="座位" prop="seat" width="90">
          <template #default="{ row }">{{ row.seat || '—' }}</template>
        </el-table-column>
        <el-table-column label="分组" prop="group_name" width="100">
          <template #default="{ row }">{{ row.group_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="问题" min-width="140">
          <template #default="{ row }">
            <span v-if="row.error" class="error-text">{{ row.error }}</span>
            <span v-else class="muted">正常</span>
          </template>
        </el-table-column>
      </el-table>

      <p v-if="invalidRows.length" class="page-hint">
        有问题的行会被自动跳过，其余照常导入。
      </p>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!validRows.length"
        @click="submit"
      >
        导入 {{ validRows.length }} 名
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.preview {
  margin-top: 14px;
}

.preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}

.error-text {
  color: #f56c6c;
}
</style>
