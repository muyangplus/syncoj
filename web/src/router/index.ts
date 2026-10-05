import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/components/AppLayout.vue'),
    children: [
      { path: '', redirect: { name: 'overview' } },
      {
        path: 'contests',
        name: 'contests',
        component: () => import('@/views/ContestsView.vue'),
        meta: { title: '场次管理' },
      },
      {
        path: 'rosters',
        name: 'rosters',
        component: () => import('@/views/RostersView.vue'),
        meta: { title: '名单库' },
      },
      {
        path: 'overview',
        name: 'overview',
        component: () => import('@/views/OverviewView.vue'),
        meta: { title: '选手状态' },
      },
      {
        path: 'machines',
        name: 'machines',
        component: () => import('@/views/MachinesView.vue'),
        meta: { title: '机器配对' },
      },
      {
        path: 'files',
        name: 'files',
        component: () => import('@/views/FilesView.vue'),
        meta: { title: '代码台账' },
      },
      {
        path: 'deploys',
        name: 'deploys',
        component: () => import('@/views/DeploysView.vue'),
        meta: { title: '文件下发' },
      },
      {
        path: 'scores',
        name: 'scores',
        component: () => import('@/views/ScoresView.vue'),
        meta: { title: '成绩' },
      },
      {
        path: 'events',
        name: 'events',
        component: () => import('@/views/EventsView.vue'),
        meta: { title: '审计日志' },
      },
      {
        path: 'releases',
        name: 'releases',
        component: () => import('@/views/ReleasesView.vue'),
        meta: { title: 'Agent 发布' },
      },
    ],
  },
  // 兜底：未知路径回首页，而不是白屏
  { path: '/:pathMatch(.*)*', redirect: { name: 'overview' } },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // 首次进入时确认已存 token 是否还有效 —— 否则刷新页面会被踢到登录页
  await auth.restore()

  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isAuthenticated) {
    return { name: 'overview' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · SyncOJ` : 'SyncOJ 管理台'
})
