<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { machineApi } from '@/api'
import { describeError } from '@/api/crud'
import type { BindingSuggestionOut } from '@/api/types'

/**
 * 配对 / 改派之后的同名建议：这台机器在**其他名单**里同名的人，一键「同时绑定」。
 *
 * 建议由服务端算（姓名去首尾空白、全半角统一后相等；重名用座位号 / 考号收窄，
 * 仍不唯一就不建议），这里只负责列出来、让教师勾选、逐条按机器配对。
 * 建议为空时不弹。
 */
const props = defineProps<{
  modelValue: boolean
  agentId: number | null
  /** 机器名，标题里用 */
  label?: string
  suggestions: BindingSuggestionOut[]
}>()

const emit = defineEmits<{ 'update:modelValue': [boolean]; done: [] }>()

const visible = computed({
  get: () => props.modelValue && props.suggestions.length > 0,
  set: (value: boolean) => emit('update:modelValue', value),
})

const picked = ref<number[]>([])
const pending = ref(false)

watch(
  () => [props.modelValue, props.suggestions] as const,
  ([open]) => {
    if (open) picked.value = props.suggestions.map((item) => item.roster_entry_id)
  },
  { immediate: true },
)

async function bindAll(): Promise<void> {
  if (!props.agentId || pending.value) return
  pending.value = true
  let bound = 0
  try {
    for (const item of props.suggestions) {
      if (!picked.value.includes(item.roster_entry_id)) continue
      try {
        await machineApi.bind(props.agentId, item.roster_entry_id)
        bound += 1
      } catch (err) {
        ElMessage.error(`${item.roster_name} · ${item.player_no}：${describeError(err)}`)
      }
    }
  } finally {
    pending.value = false
  }
  if (bound) ElMessage.success(`已同时绑定 ${bound} 份名单`)
  emit('update:modelValue', false)
  emit('done')
}
</script>

<template>
  <el-dialog v-model="visible" :title="`同名建议 · ${label || ''}`" width="620px">
    <el-checkbox-group v-model="picked" class="list">
      <el-checkbox
        v-for="item in suggestions"
        :key="item.roster_entry_id"
        :value="item.roster_entry_id"
        class="row"
      >
        <span class="target">{{ item.roster_name }} · {{ item.player_no }} {{ item.name || '' }}</span>
        <span v-if="item.seat" class="cell-sub"> · 座位 {{ item.seat }}</span>
        <span class="cell-sub"> ← {{ item.source }}</span>
      </el-checkbox>
    </el-checkbox-group>
    <template #footer>
      <el-button @click="visible = false">跳过</el-button>
      <el-button type="primary" :disabled="!picked.length" :loading="pending" @click="bindAll">
        同时绑定（{{ picked.length }}）
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.target {
  font-weight: 600;
}
</style>
