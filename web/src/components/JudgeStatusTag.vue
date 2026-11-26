<script setup lang="ts">
/** 评测状态标签：显示代号，悬停「名称：说明」（§12.16，词表在 utils/judgeStatus.ts）。 */
import { computed } from 'vue'

import { statusCode, statusTone, statusTooltip } from '@/utils/judgeStatus'

const props = withDefaults(defineProps<{ code: string | null | undefined; size?: 'small' | 'default'; clickable?: boolean }>(), {
  size: 'small',
  clickable: false,
})
const emit = defineEmits<{ (e: 'click'): void }>()

const tone = computed(() => statusTone(props.code))
const tooltip = computed(() => statusTooltip(props.code))
</script>

<template>
  <el-tooltip v-if="code" :content="tooltip" placement="top" :show-after="300">
    <el-tag
      :type="tone === 'primary' ? 'primary' : tone"
      :size="size"
      effect="light"
      :class="{ 'is-clickable': clickable }"
      @click="clickable && emit('click')"
    >
      {{ statusCode(code) }}
    </el-tag>
  </el-tooltip>
  <span v-else class="muted">—</span>
</template>

<style scoped>
.is-clickable {
  cursor: pointer;
  text-decoration: underline dotted;
}
</style>
