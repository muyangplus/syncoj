<script setup lang="ts">
/** 页面里的一个区块：标题、计数、右侧操作、正文。 */
defineProps<{
  title?: string
  /** 标题后的灰色计数或补充，如「共 12 条」 */
  meta?: string | number | null
  /** 正文不留内边距（整块是表格时用） */
  flush?: boolean
}>()
</script>

<template>
  <section class="section-card" :class="{ flush }">
    <header v-if="title || $slots.title || $slots.extra" class="section-head">
      <div class="section-heading">
        <slot name="title">
          <h3 class="section-card-title">{{ title }}</h3>
        </slot>
        <span v-if="meta !== undefined && meta !== null && meta !== ''" class="section-meta">{{ meta }}</span>
      </div>
      <div class="section-extra">
        <slot name="extra" />
      </div>
    </header>
    <div class="section-body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.section-card {
  background: var(--sj-surface);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius);
  box-shadow: var(--sj-shadow);
}
.section-card + .section-card {
  margin-top: 16px;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 14px 20px;
  border-bottom: 1px solid var(--sj-border);
}
.section-heading {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.section-card-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.section-meta {
  font-size: 12.5px;
  color: var(--sj-text-3);
}
.section-extra {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.section-body {
  padding: 16px 20px;
}
.flush .section-body {
  padding: 0;
}
.flush .section-body :deep(.el-table) {
  border-radius: 0 0 var(--sj-radius) var(--sj-radius);
}
</style>
