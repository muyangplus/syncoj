import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

import { publicMode } from '@/publicMode'
import { useAuthStore } from '@/stores/auth'

/**
 * 这一份前端是不是跑在**公开端口**上（默认 80，只提供考生页与装机页）。
 *
 * 由服务端注入（`window.__SYNCOJ__`），前端**不自己数端口** —— 那等于把"哪个端口
 * 是公开端口"定义两遍，而且 http 的 80 在浏览器里 `location.port === ""`，数也数
 * 不准。细节见 `@/publicMode`。
 */
const publicOnly = publicMode.publicOnly === true

/** 没指定去哪时该去哪一页：公开端口上是考生页，管理端是总览。 */
const homeRoute = () => (publicOnly ? { name: 'player' } : { name: 'overview' })

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    // 装机页：给**还没装 Agent 的机器**和站在它前面的教师用，所以免登录。
    // 与登录页一样是顶层路由 —— 不能放进 AppLayout，那是管理端的壳子。
    path: '/install',
    name: 'install',
    component: () => import('@/views/InstallView.vue'),
    meta: { public: true, title: '装机' },
  },
  {
    // 选手页：选手不登录，所以也免登录。身份靠"场次 + 考号"两个参数。
    path: '/player',
    name: 'player',
    component: () => import('@/views/PlayerView.vue'),
    meta: { public: true, title: '考生须知' },
  },
  {
    path: '/',
    component: () => import('@/components/AppLayout.vue'),
    children: [
      { path: '', redirect: homeRoute },
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
  { path: '/:pathMatch(.*)*', redirect: homeRoute },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  if (publicOnly) {
    // 公开端口上没有管理台，所以也**不去碰鉴权状态** —— 那会去打管理接口，
    // 而那个接口在这个端口上是被挡掉的（404），只会白等一次请求。
    // 登录页同样拦掉：这里没有登录入口，让学生看见一个登录框只会更困惑。
    if (to.meta.public && to.name !== 'login') {
      return true
    }
    return { name: 'player' }
  }

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
