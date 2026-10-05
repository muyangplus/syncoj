<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { authApi } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useContestStore } from '@/stores/contest'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const contests = useContestStore()

const serverOk = ref<boolean | null>(null)
const onlineCount = ref(0)
const totalCount = ref(0)

const currentTitle = computed(() => (route.meta.title as string | undefined) ?? '')

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

function handleContestChange(value: number): void {
  contests.select(value)
}

async function handleLogout(): Promise<void> {
  await auth.logout()
  ElMessage.success('已退出登录')
  await router.replace({ name: 'login' })
}

onMounted(async () => {
  await Promise.all([refreshHealth(), contests.load().catch(() => undefined)])
  // 健康状态每 10 秒刷一次：顶部那行"在线 3/50"是教师最常瞄的信息
  window.setInterval(() => void refreshHealth(), 10000)
})
</script>

<template>
  <el-container class="layout">
    <el-aside width="200px" class="sidebar">
      <div class="brand">
        <span class="brand-name">SyncOJ</span>
        <span class="brand-sub">在线同步评测</span>
      </div>

      <el-menu
        :default-active="String(route.name ?? '')"
        router
        background-color="transparent"
        text-color="var(--syncoj-sidebar-fg)"
        active-text-color="#fff"
        class="nav"
      >
        <el-menu-item index="overview" :route="{ name: 'overview' }">
          <el-icon><Monitor /></el-icon><span>选手状态</span>
        </el-menu-item>
        <el-menu-item index="files" :route="{ name: 'files' }">
          <el-icon><Document /></el-icon><span>代码台账</span>
        </el-menu-item>
        <el-menu-item index="deploys" :route="{ name: 'deploys' }">
          <el-icon><UploadFilled /></el-icon><span>文件下发</span>
        </el-menu-item>
        <el-menu-item index="scores" :route="{ name: 'scores' }">
          <el-icon><Trophy /></el-icon><span>成绩</span>
        </el-menu-item>
        <el-menu-item index="events" :route="{ name: 'events' }">
          <el-icon><Bell /></el-icon><span>审计日志</span>
        </el-menu-item>
        <el-menu-item index="releases" :route="{ name: 'releases' }">
          <el-icon><Refresh /></el-icon><span>Agent 发布</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div class="topbar-left">
          <h1 class="topbar-title">{{ currentTitle }}</h1>
          <el-tag v-if="contests.current" size="small" type="info" effect="plain">
            {{ contests.current.name }}
          </el-tag>
        </div>

        <div class="topbar-right">
          <el-tooltip
            :content="
              serverOk === false
                ? '服务端无响应'
                : `在线 ${onlineCount} / 共 ${totalCount} 台`
            "
          >
            <span class="health">
              <span
                class="status-dot"
                :class="serverOk === false ? 'offline' : 'online'"
              />
              <span class="muted">{{ onlineCount }}/{{ totalCount }}</span>
            </span>
          </el-tooltip>

          <el-select
            v-if="contests.contests.length"
            :model-value="contests.currentId ?? undefined"
            size="small"
            style="width: 180px"
            placeholder="选择场次"
            @update:model-value="handleContestChange"
          >
            <el-option
              v-for="contest in contests.contests"
              :key="contest.id"
              :label="contest.name"
              :value="contest.id"
            />
          </el-select>
          <el-button
            v-else-if="contests.error"
            size="small"
            type="warning"
            plain
            @click="contests.load().catch(() => undefined)"
          >
            场次加载失败，重试
          </el-button>

          <el-dropdown @command="handleLogout">
            <span class="user">
              <el-icon><User /></el-icon>
              {{ auth.username ?? '未登录' }}
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="content">
        <!-- 没有场次时几乎所有页面都没意义，这里统一拦一次并给出下一步 -->
        <el-alert
          v-if="!contests.loading && contests.contests.length === 0"
          type="warning"
          :closable="false"
          show-icon
          title="还没有任何场次"
          description="请先创建一个场次并导入选手，之后各页面才会有数据。"
          style="margin-bottom: 16px"
        />
        <RouterView v-else />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.sidebar {
  background: var(--syncoj-sidebar-bg);
  display: flex;
  flex-direction: column;
}

.brand {
  padding: 18px 20px 14px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.brand-name {
  color: #fff;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.brand-sub {
  color: #7d8fa3;
  font-size: 11px;
}

.nav {
  border-right: none;
  flex: 1;
}

.nav :deep(.el-menu-item.is-active) {
  background: rgba(64, 158, 255, 0.16) !important;
  border-left: 3px solid var(--syncoj-sidebar-active);
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  height: 56px;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.topbar-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

.health {
  display: inline-flex;
  align-items: center;
  cursor: default;
}

.user {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: #606266;
  font-size: 13px;
  outline: none;
}

.content {
  background: var(--syncoj-bg);
  padding: 16px;
  overflow: auto;
}
</style>
