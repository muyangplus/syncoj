<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { authApi } from '@/api'
import AboutDialog from '@/components/AboutDialog.vue'
import CommandPalette from '@/components/CommandPalette.vue'
import { usePolling } from '@/composables/usePolling'
import { NAV_GROUPS, useQuickActions } from '@/composables/useQuickActions'
import type { NavItem } from '@/composables/useQuickActions'
import { useTheme } from '@/composables/useTheme'
import { useAuthStore } from '@/stores/auth'
import { useContestStore } from '@/stores/contest'
import { contestStatusLabel, contestStatusType } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const contests = useContestStore()
const { theme, toggle: toggleTheme } = useTheme()
const { actions } = useQuickActions()

const serverOk = ref<boolean | null>(null)
const onlineCount = ref(0)
const totalCount = ref(0)
const palette = ref<InstanceType<typeof CommandPalette> | null>(null)
const collapsed = ref(false)
const aboutOpen = ref(false)

function covers(item: NavItem, name: unknown): boolean {
  return item.name === name || !!item.children?.some((child) => child.name === name)
}

const currentGroup = computed(() => {
  for (const group of NAV_GROUPS) {
    if (group.items.some((item) => covers(item, route.name))) return group.title
  }
  return ''
})

/** 侧栏里展开着的父项（按 name）。当前页在某个父项下时默认展开。 */
const expanded = ref<Record<string, boolean>>({})
function isExpanded(item: NavItem): boolean {
  return expanded.value[item.name] ?? covers(item, route.name)
}
function toggleExpanded(item: NavItem): void {
  expanded.value = { ...expanded.value, [item.name]: !isExpanded(item) }
}
const currentTitle = computed(() => (route.meta.title as string | undefined) ?? '')

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

async function refreshHealth(): Promise<void> {
  try {
    const health = await authApi.health()
    serverOk.value = health.ok
    onlineCount.value = health.agents_online
    totalCount.value = health.agents_total
  } catch {
    serverOk.value = false
  }
}

function runAction(key: string): void {
  const action = actions.find((item) => item.key === key)
  if (action) void action.run()
}

async function handleUser(command: string): Promise<void> {
  if (command === 'about') {
    aboutOpen.value = true
    return
  }
  if (command === 'logout') {
    await auth.logout()
    ElMessage.success('已退出登录')
    await router.replace({ name: 'login' })
  }
}

const health = usePolling(refreshHealth, { interval: 10000, immediate: false })

onMounted(async () => {
  await Promise.all([health.refresh(), contests.load().catch(() => undefined)])
})
</script>

<template>
  <div class="shell" :class="{ collapsed }">
    <aside class="sidebar">
      <div class="brand" @click="router.push({ name: 'dashboard' })">
        <span class="brand-mark">S</span>
        <span v-if="!collapsed" class="brand-text">
          <span class="brand-name">SyncOJ</span>
          <span class="brand-sub">考场同步评测</span>
        </span>
      </div>

      <nav class="nav">
        <div v-for="group in NAV_GROUPS" :key="group.title" class="nav-group">
          <div v-if="!collapsed" class="nav-group-title">{{ group.title }}</div>
          <template v-for="item in group.items" :key="item.name">
            <!-- 有二级项：展开侧栏时可折叠；收起时悬停弹出二级项，点图标去第一项 -->
            <template v-if="item.children?.length">
              <el-popover
                v-if="collapsed"
                placement="right-start"
                trigger="hover"
                :width="160"
                :show-arrow="false"
              >
                <template #reference>
                  <RouterLink
                    :to="{ name: item.children[0].name }"
                    class="nav-item"
                    :class="{ active: covers(item, route.name) }"
                  >
                    <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
                  </RouterLink>
                </template>
                <div class="nav-pop">
                  <RouterLink
                    v-for="child in item.children"
                    :key="child.name"
                    :to="{ name: child.name }"
                    class="nav-item nav-child"
                    :class="{ active: route.name === child.name }"
                  >
                    <span class="nav-label">{{ child.label }}</span>
                  </RouterLink>
                </div>
              </el-popover>
              <template v-else>
                <button
                  type="button"
                  class="nav-parent"
                  :class="{ 'parent-active': covers(item, route.name) }"
                  @click="toggleExpanded(item)"
                >
                  <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
                  <span class="nav-label">{{ item.label }}</span>
                  <el-icon class="nav-caret" :class="{ open: isExpanded(item) }"><ArrowRight /></el-icon>
                </button>
                <template v-if="isExpanded(item)">
                  <RouterLink
                    v-for="child in item.children"
                    :key="child.name"
                    :to="{ name: child.name }"
                    class="nav-item nav-child"
                    :class="{ active: route.name === child.name }"
                  >
                    <span class="nav-label">{{ child.label }}</span>
                  </RouterLink>
                </template>
              </template>
            </template>
            <RouterLink
              v-else
              :to="{ name: item.name }"
              class="nav-item"
              :class="{ active: route.name === item.name }"
              :title="collapsed ? item.label : undefined"
            >
              <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
              <span v-if="!collapsed" class="nav-label">{{ item.label }}</span>
            </RouterLink>
          </template>
        </div>
      </nav>

      <div class="sidebar-foot">
        <button type="button" class="foot-btn" :title="collapsed ? '展开侧栏' : '收起侧栏'" @click="collapsed = !collapsed">
          <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
        </button>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="crumbs">
          <span v-if="currentGroup" class="crumb-group">{{ currentGroup }}</span>
          <el-icon v-if="currentGroup" class="crumb-sep"><ArrowRight /></el-icon>
          <span class="crumb-page">{{ currentTitle }}</span>
        </div>

        <button type="button" class="search" @click="palette?.show()">
          <el-icon><Search /></el-icon>
          <span>搜索页面或操作</span>
          <kbd>{{ isMac ? '⌘' : 'Ctrl' }} K</kbd>
        </button>

        <div class="topbar-right">
          <el-select
            v-if="contests.contests.length"
            :model-value="contests.currentId ?? undefined"
            class="contest-select"
            placeholder="选择场次"
            @update:model-value="(value: number) => contests.select(value)"
          >
            <template #prefix>
              <el-icon><Calendar /></el-icon>
            </template>
            <el-option v-for="item in contests.options" :key="item.id" :label="item.name" :value="item.id">
              <div class="contest-option">
                <span>{{ item.name }}</span>
                <el-tag size="small" :type="contestStatusType(item.status)" effect="plain">
                  {{ contestStatusLabel(item.status) }}
                </el-tag>
              </div>
            </el-option>
            <template #footer>
              <div class="contest-footer">
                <el-button link type="primary" size="small" @click="router.push({ name: 'contests', query: { do: 'new' } })">
                  <el-icon><Plus /></el-icon>新建场次
                </el-button>
                <el-checkbox
                  v-if="contests.archived.length"
                  size="small"
                  :model-value="contests.showArchived"
                  @update:model-value="(value: string | number | boolean) => contests.setShowArchived(!!value)"
                >
                  显示已归档（{{ contests.archived.length }}）
                </el-checkbox>
              </div>
            </template>
          </el-select>
          <el-button
            v-else-if="contests.error"
            type="warning"
            plain
            @click="contests.load().catch(() => undefined)"
          >
            场次加载失败，重试
          </el-button>
          <el-button
            v-else-if="!contests.loading"
            type="primary"
            @click="router.push({ name: 'contests', query: { do: 'new' } })"
          >
            新建场次
          </el-button>

          <el-tooltip :content="serverOk === false ? '服务端无响应' : '在线机器 / 已注册机器'">
            <button type="button" class="pill" @click="router.push({ name: 'overview' })">
              <span class="status-dot" :class="serverOk === false ? 'offline' : 'online'" />
              {{ onlineCount }} / {{ totalCount }}
            </button>
          </el-tooltip>

          <el-dropdown trigger="click" @command="runAction">
            <el-button type="primary">
              <el-icon><Lightning /></el-icon>
              <span>快捷操作</span>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu class="quick-menu">
                <el-dropdown-item
                  v-for="(action, index) in actions"
                  :key="action.key"
                  :command="action.key"
                  :disabled="action.needsContest && !contests.currentId"
                  :divided="index > 0 && actions[index - 1].group !== action.group"
                >
                  <el-icon><component :is="action.icon" /></el-icon>{{ action.label }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-tooltip :content="theme === 'dark' ? '切换到浅色' : '切换到深色'">
            <button type="button" class="icon-btn" @click="toggleTheme">
              <el-icon><component :is="theme === 'dark' ? 'Sunny' : 'Moon'" /></el-icon>
            </button>
          </el-tooltip>

          <el-dropdown trigger="click" @command="handleUser">
            <button type="button" class="avatar" :title="auth.username ?? ''">
              {{ (auth.username ?? '?').slice(0, 1).toUpperCase() }}
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>{{ auth.username ?? '未登录' }}</el-dropdown-item>
                <el-dropdown-item command="about">
                  <el-icon><InfoFilled /></el-icon>关于与许可
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="content">
        <!-- 始终渲染 RouterView：没有场次时也要能进「场次管理」新建 -->
        <el-alert
          v-if="!contests.loading && contests.contests.length === 0 && route.name !== 'contests'"
          type="warning"
          :closable="false"
          show-icon
          class="no-contest"
        >
          <template #title>
            尚无场次。
            <el-button link type="primary" @click="router.push({ name: 'contests', query: { do: 'new' } })">
              新建场次
            </el-button>
          </template>
        </el-alert>
        <RouterView />
      </main>
    </div>

    <CommandPalette ref="palette" />
    <AboutDialog v-model="aboutOpen" />
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100%;
}

/* ---- 侧栏 ---- */

.sidebar {
  width: var(--sj-sidebar-w);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--sj-surface);
  border-right: 1px solid var(--sj-border);
  transition: width 0.18s ease;
}
.collapsed .sidebar {
  width: 68px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--sj-topbar-h);
  padding: 0 18px;
  cursor: pointer;
  border-bottom: 1px solid var(--sj-border);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 9px;
  background: var(--sj-brand-gradient);
  color: #fff;
  font-weight: 700;
  font-size: 16px;
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
}
.brand-name {
  font-weight: 700;
  font-size: 16px;
  letter-spacing: -0.01em;
}
.brand-sub {
  font-size: 11.5px;
  color: var(--sj-text-3);
}

.nav {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
}
.nav-group + .nav-group {
  margin-top: 14px;
}
.nav-group-title {
  padding: 0 10px 6px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--sj-text-3);
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 38px;
  padding: 0 10px;
  border-radius: 8px;
  color: var(--sj-text-2);
  text-decoration: none;
  font-size: 14px;
  transition: background 0.12s, color 0.12s;
}
.collapsed .nav-item {
  justify-content: center;
  padding: 0;
}
.nav-item:hover {
  background: var(--sj-surface-2);
  color: var(--sj-text);
}
.nav-item.active {
  background: var(--sj-primary-soft);
  color: var(--sj-primary);
  font-weight: 600;
}
.nav-icon {
  font-size: 17px;
}
.nav-parent {
  all: unset;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 38px;
  padding: 0 10px;
  border-radius: 8px;
  color: var(--sj-text-2);
  font-size: 14px;
  cursor: pointer;
}
.nav-parent:hover {
  background: var(--sj-surface-2);
  color: var(--sj-text);
}
.nav-parent.parent-active {
  color: var(--sj-primary);
}
.nav-caret {
  margin-left: auto;
  font-size: 12px;
  transition: transform 0.15s;
}
.nav-caret.open {
  transform: rotate(90deg);
}
.nav-child {
  height: 34px;
  padding-left: 37px;
  font-size: 13.5px;
}
.nav-pop .nav-child {
  padding-left: 10px;
}
.contest-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.sidebar-foot {
  padding: 10px 12px;
  border-top: 1px solid var(--sj-border);
}
.foot-btn {
  all: unset;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 32px;
  border-radius: 8px;
  color: var(--sj-text-3);
  cursor: pointer;
}
.foot-btn:hover {
  background: var(--sj-surface-2);
  color: var(--sj-text);
}

/* ---- 顶栏 ---- */

.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  height: var(--sj-topbar-h);
  padding: 0 24px;
  background: var(--sj-surface);
  border-bottom: 1px solid var(--sj-border);
  flex-shrink: 0;
}
.crumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  white-space: nowrap;
}
.crumb-group {
  color: var(--sj-text-3);
}
.crumb-sep {
  color: var(--sj-text-3);
  font-size: 12px;
}
.crumb-page {
  font-weight: 600;
}

.search {
  all: unset;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  width: 260px;
  padding: 0 10px;
  margin-left: 12px;
  border: 1px solid var(--sj-border);
  border-radius: 8px;
  background: var(--sj-surface-2);
  color: var(--sj-text-3);
  font-size: 13px;
  cursor: pointer;
}
.search span {
  flex: 1;
}
.search:hover {
  border-color: var(--sj-border-strong);
}
.search kbd {
  font-family: var(--sj-mono);
  font-size: 11px;
  padding: 1px 6px;
  border: 1px solid var(--sj-border-strong);
  border-radius: 4px;
}

.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}
.contest-select {
  width: 220px;
}
.contest-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.pill {
  all: unset;
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--sj-border);
  border-radius: 999px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  color: var(--sj-text-2);
  cursor: pointer;
}
.pill:hover {
  border-color: var(--sj-border-strong);
}
.icon-btn {
  all: unset;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  color: var(--sj-text-2);
  cursor: pointer;
  font-size: 17px;
}
.icon-btn:hover {
  background: var(--sj-surface-2);
}
.avatar {
  all: unset;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--sj-primary-soft);
  color: var(--sj-primary);
  font-weight: 600;
  cursor: pointer;
}

.content {
  flex: 1;
  overflow: auto;
  padding: 24px 28px 40px;
}
.no-contest {
  margin-bottom: 16px;
}

@media (max-width: 1100px) {
  .search {
    display: none;
  }
}
@media (max-width: 860px) {
  .sidebar {
    width: 68px;
  }
  .nav-label,
  .nav-group-title,
  .brand-text {
    display: none;
  }
  .contest-select {
    width: 150px;
  }
  .content {
    padding: 16px;
  }
}
</style>

<style>
.quick-menu .el-dropdown-menu__item {
  gap: 2px;
  min-width: 180px;
}
</style>
