<script setup lang="ts">
/** 命令面板（Ctrl/⌘+K）：搜索页面与快捷操作。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { NAV_GROUPS, useQuickActions } from '@/composables/useQuickActions'
import { useContestStore } from '@/stores/contest'

const open = ref(false)
const query = ref('')
const active = ref(0)
const input = ref<HTMLInputElement | null>(null)

const router = useRouter()
const contest = useContestStore()
const { actions } = useQuickActions()

interface Entry {
  key: string
  label: string
  icon: string
  kind: '页面' | '操作'
  disabled: boolean
  run: () => void | Promise<void>
}

const entries = computed<Entry[]>(() => [
  ...actions.map((action) => ({
    key: `a:${action.key}`,
    label: action.label,
    icon: action.icon,
    kind: '操作' as const,
    disabled: !!action.needsContest && !contest.currentId,
    run: action.run,
  })),
  ...NAV_GROUPS.flatMap((group) => group.items).map((item) => ({
    key: `p:${item.name}`,
    label: item.label,
    icon: item.icon,
    kind: '页面' as const,
    disabled: false,
    run: () => {
      void router.push({ name: item.name })
    },
  })),
])

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  return entries.value.filter((entry) => !q || entry.label.toLowerCase().includes(q))
})

watch(filtered, () => {
  active.value = 0
})

function show(): void {
  open.value = true
  query.value = ''
  active.value = 0
  void nextTick(() => input.value?.focus())
}

async function choose(entry: Entry | undefined): Promise<void> {
  if (!entry || entry.disabled) return
  open.value = false
  await entry.run()
}

function onKey(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    if (open.value) open.value = false
    else show()
  }
}

function onInputKey(event: KeyboardEvent): void {
  const count = filtered.value.length
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    active.value = count ? (active.value + 1) % count : 0
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    active.value = count ? (active.value - 1 + count) % count : 0
  } else if (event.key === 'Enter') {
    event.preventDefault()
    void choose(filtered.value[active.value])
  } else if (event.key === 'Escape') {
    open.value = false
  }
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

defineExpose({ show })
</script>

<template>
  <el-dialog v-model="open" width="560px" :show-close="false" class="palette" align-center>
    <div class="palette-search">
      <el-icon class="palette-search-icon"><Search /></el-icon>
      <input
        ref="input"
        v-model="query"
        class="palette-input"
        placeholder="搜索页面或操作"
        @keydown="onInputKey"
      />
      <kbd>Esc</kbd>
    </div>
    <ul class="palette-list">
      <li
        v-for="(entry, index) in filtered"
        :key="entry.key"
        :class="{ active: index === active, disabled: entry.disabled }"
        @mouseenter="active = index"
        @click="choose(entry)"
      >
        <el-icon class="palette-icon"><component :is="entry.icon" /></el-icon>
        <span class="palette-label">{{ entry.label }}</span>
        <span v-if="entry.disabled" class="palette-kind">需先选场次</span>
        <span v-else class="palette-kind">{{ entry.kind }}</span>
      </li>
      <li v-if="!filtered.length" class="palette-empty">无匹配项</li>
    </ul>
  </el-dialog>
</template>

<style scoped>
.palette-search {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 4px 12px;
  border-bottom: 1px solid var(--sj-border);
}
.palette-search-icon {
  color: var(--sj-text-3);
  font-size: 18px;
}
.palette-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  font: inherit;
  font-size: 16px;
  color: var(--sj-text);
}
kbd {
  font-family: var(--sj-mono);
  font-size: 11px;
  padding: 2px 6px;
  border: 1px solid var(--sj-border-strong);
  border-radius: 4px;
  color: var(--sj-text-3);
}
.palette-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  max-height: 380px;
  overflow: auto;
}
.palette-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  color: var(--sj-text);
}
.palette-list li.active {
  background: var(--sj-primary-soft);
  color: var(--sj-primary);
}
.palette-list li.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.palette-icon {
  font-size: 16px;
}
.palette-label {
  flex: 1;
}
.palette-kind {
  font-size: 12px;
  color: var(--sj-text-3);
}
.palette-empty {
  justify-content: center;
  color: var(--sj-text-3);
  cursor: default;
}
</style>

<style>
.palette .el-dialog__header {
  display: none;
}
.palette .el-dialog__body {
  padding: 14px 14px 10px;
}
</style>
