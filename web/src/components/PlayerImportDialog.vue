<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { playerApi } from '@/api'
import type { PlayerOut, PlayerUpsert } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useMutation } from '@/composables/useMutation'
import { parsePasteLines } from '@/utils/paste'

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
 * 分隔符的尝试顺序（制表符 → 逗号 → 空白）见 ``utils/paste.ts``：
 * 教师多半是从 Excel 粘过来的（制表符），也可能是手敲的。
 */
const parsed = computed<ParsedRow[]>(() => {
  const seen = new Set<string>()
  const rows: ParsedRow[] = []

  for (const { line, columns } of parsePasteLines(raw.value)) {
    const [playerNo = '', name = '', seat = '', group = ''] = columns

    const row: ParsedRow = {
      line,
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
  }

  return rows
})

const validRows = computed(() => parsed.value.filter((row) => !row.error))
const invalidRows = computed(() => parsed.value.filter((row) => row.error))

// 每次打开都从空白开始：留着上一次的粘贴内容会让"再导一次"变成误操作
watch(visible, (open) => {
  if (open) raw.value = ''
})

/**
 * 导入接口返回的是**动作结果**，不是一页数据（`docs/api-conventions.md` §2）：
 * 服务端回的是 `{created, updated, players}` —— "新建了几条、更新了几条"才是
 * 教师想看的。`players` 里是受影响的行，紧随其后的"应用名单/批量配对"要用
 * 它们的 id，不该再查一次。
 *
 * 类型就是 `PlayerImportOut`，与 `playerApi.import` 的返回一致 —— 这里不再
 * 需要任何断言。
 */
const importPlayers = useMutation(
  (payload: PlayerUpsert[]) => {
    if (!props.contestId) throw new Error('请先选择场次')
    if (!payload.length) throw new Error('没有可导入的选手')
    return playerApi.import(props.contestId, payload)
  },
  {
    success: (result) => `已导入：新建 ${result.created} 名，更新 ${result.updated} 名`,
    onDone: (result) => {
      visible.value = false
      // `players` 在生成物里是可选的（服务端给了默认值 default_factory），
      // 但这条路径上它一定有值 —— 兜一个空数组免得下游要处理 undefined
      emit('imported', result.players ?? [])
    },
  },
)

function submit(): void {
  void importPlayers.run(
    validRows.value.map((row) => ({
      player_no: row.player_no,
      name: row.name || null,
      seat: row.seat || null,
      group_name: row.group_name || null,
    })),
  )
}
</script>

<template>
  <FormDialog
    v-model="visible"
    title="导入选手"
    width="720px"
    :submitting="importPlayers.pending.value"
    :disabled="!validRows.length"
    :confirm-text="`导入 ${validRows.length} 名`"
    @submit="submit"
  >
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>每行一名选手：<code>编号,姓名,座位,分组</code></template>
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
    </div>
  </FormDialog>
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
