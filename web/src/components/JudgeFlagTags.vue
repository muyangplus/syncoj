<script setup lang="ts">
/** 评测结果的风险 / 疑似违规计数（§12.11）：红色在前，黄色在后，没有就不显示。 */
defineProps<{ warning?: number | null; risk?: number | null }>()
</script>

<template>
  <span v-if="risk || warning" class="flag-tags">
    <span v-if="risk" class="flag flag-risk">风险 {{ risk }}</span>
    <span v-if="warning" class="flag flag-warning">疑似 {{ warning }}</span>
  </span>
</template>

<style scoped>
.flag-tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  vertical-align: middle;
}
.flag {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 6px;
  border-radius: var(--sj-radius-sm);
  border: 1px solid;
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
}
.flag-risk {
  color: var(--sj-danger);
  border-color: var(--sj-danger-line);
  background: var(--sj-danger-soft);
}
.flag-warning {
  color: var(--sj-warning);
  border-color: var(--sj-warning-line);
  background: var(--sj-warning-soft);
}
</style>
