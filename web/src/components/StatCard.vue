<script setup lang="ts">
/** 工作台上的一个数字卡片。 */
defineProps<{
  label: string
  value: string | number
  /** 数字下方的一行补充 */
  sub?: string
  icon?: string
  tone?: 'primary' | 'success' | 'warning' | 'danger' | 'info'
  /** 0-100，给了就在底部画进度条 */
  progress?: number | null
}>()
defineEmits<{ click: [] }>()
</script>

<template>
  <button type="button" class="stat" :class="tone ?? 'primary'" @click="$emit('click')">
    <div class="stat-top">
      <span class="stat-label">{{ label }}</span>
      <span v-if="icon" class="stat-icon"><el-icon><component :is="icon" /></el-icon></span>
    </div>
    <div class="stat-value">{{ value }}</div>
    <div v-if="sub" class="stat-sub">{{ sub }}</div>
    <div v-if="progress !== undefined && progress !== null" class="stat-bar">
      <span :style="{ width: `${Math.max(0, Math.min(100, progress))}%` }" />
    </div>
  </button>
</template>

<style scoped>
.stat {
  all: unset;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 16px 18px;
  background: var(--sj-surface);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius);
  box-shadow: var(--sj-shadow);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
  min-width: 0;
}
.stat:hover {
  border-color: var(--sj-border-strong);
  box-shadow: var(--sj-shadow-lg);
  transform: translateY(-1px);
}
.stat:focus-visible {
  outline: 2px solid var(--sj-primary);
  outline-offset: 2px;
}
.stat-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.stat-label {
  font-size: 13px;
  color: var(--sj-text-2);
}
.stat-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  font-size: 16px;
}
.stat-value {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
  line-height: 1.2;
}
.stat-sub {
  font-size: 12px;
  color: var(--sj-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stat-bar {
  margin-top: 8px;
  height: 4px;
  border-radius: 999px;
  background: var(--sj-info-soft);
  overflow: hidden;
}
.stat-bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: currentColor;
}
.primary .stat-icon { background: var(--sj-primary-soft); color: var(--sj-primary); }
.success .stat-icon { background: var(--sj-success-soft); color: var(--sj-success); }
.warning .stat-icon { background: var(--sj-warning-soft); color: var(--sj-warning); }
.danger .stat-icon { background: var(--sj-danger-soft); color: var(--sj-danger); }
.info .stat-icon { background: var(--sj-info-soft); color: var(--sj-text-2); }
.primary .stat-bar { color: var(--sj-primary); }
.success .stat-bar { color: var(--sj-success); }
.warning .stat-bar { color: var(--sj-warning); }
.danger .stat-bar { color: var(--sj-danger); }
.info .stat-bar { color: var(--sj-text-3); }
</style>
