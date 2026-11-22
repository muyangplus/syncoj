/**
 * 全局快捷操作：顶栏「快捷操作」菜单、命令面板（Ctrl/⌘+K）、工作台共用同一份清单。
 *
 * 需要弹窗的操作跳到对应页面并带 `?do=<动作>`，由页面打开自己的弹窗；
 * 一步完成的操作（召集机器、重扫成绩、全部评测）就地调接口。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'

import { judgingApi, machineApi, scoreApi } from '@/api'
import { ApiError } from '@/api/client'
import { useContestStore } from '@/stores/contest'

export interface QuickAction {
  key: string
  label: string
  /** Element Plus 图标组件名 */
  icon: string
  group: '机器' | '考务' | '代码与评测'
  /** 需要当前场次 */
  needsContest?: boolean
  run: () => Promise<void> | void
}

/** 页面导航项（命令面板也能搜到）。 */
export interface NavItem {
  name: string
  label: string
  icon: string
  group: string
  /** 二级项：侧栏里可展开；命令面板逐项可搜 */
  children?: NavItem[]
  /** 命令面板额外匹配的词 */
  keywords?: string
}

/** 侧栏 / 命令面板里真正能跳转的页面（有二级项的父项只列它的二级项）。 */
export function navPages(): NavItem[] {
  return NAV_GROUPS.flatMap((group) =>
    group.items.flatMap((item) =>
      item.children?.length
        ? item.children.map((child) => ({ ...child, label: `${item.label} · ${child.label}` }))
        : [item],
    ),
  )
}

export const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: '总览',
    items: [{ name: 'dashboard', label: '工作台', icon: 'Odometer', group: '总览' }],
  },
  {
    title: '考务',
    items: [
      {
        name: 'contests',
        label: '场次管理',
        icon: 'Calendar',
        group: '考务',
        children: [
          { name: 'contests', label: '全部场次', icon: 'Calendar', group: '考务' },
          {
            name: 'contests-archived',
            label: '已归档',
            icon: 'Box',
            group: '考务',
            keywords: '已归档场次 归档',
          },
        ],
      },
      { name: 'rosters', label: '名单库', icon: 'Notebook', group: '考务' },
      { name: 'overview', label: '选手状态', icon: 'Monitor', group: '考务' },
    ],
  },
  {
    title: '机器',
    items: [
      { name: 'machines', label: '机器配对', icon: 'Connection', group: '机器' },
      { name: 'releases', label: 'Agent 发布', icon: 'Box', group: '机器' },
    ],
  },
  {
    title: '代码与评测',
    items: [
      { name: 'files', label: '代码台账', icon: 'Document', group: '代码与评测' },
      { name: 'deploys', label: '文件下发', icon: 'UploadFilled', group: '代码与评测' },
      { name: 'judge', label: '评测', icon: 'Cpu', group: '代码与评测' },
      { name: 'scores', label: '成绩', icon: 'Trophy', group: '代码与评测' },
    ],
  },
  {
    title: '系统',
    items: [{ name: 'events', label: '审计日志', icon: 'Bell', group: '系统' }],
  },
]

export function useQuickActions() {
  const router = useRouter()
  const contest = useContestStore()

  function go(name: string, action?: string): void {
    void router.push({ name, query: action ? { do: action } : {} })
  }

  async function announce(): Promise<void> {
    try {
      const status = await machineApi.announce()
      if (status.last_error) {
        ElMessage.warning(`召集未发出：${status.last_error}`)
      } else {
        ElMessage.success(`已向局域网广播服务端地址 ${status.last_url ?? ''}`.trim())
      }
    } catch (err) {
      ElMessage.error((err as Error).message)
    }
  }

  async function rescan(): Promise<void> {
    const contestId = contest.currentId
    if (!contestId) return
    try {
      const report = await scoreApi.rescan(contestId)
      ElMessage.success(`重扫完成：解析 ${report.parsed}，未解析 ${report.unparsed}，未变 ${report.unchanged}`)
    } catch (err) {
      ElMessage.error((err as Error).message)
    }
  }

  async function judgeAll(force = false): Promise<void> {
    const contestId = contest.currentId
    if (!contestId) return
    try {
      const result = await judgingApi.queue(contestId, { force })
      ElMessage.success(`已排队 ${result.queued} 个评测任务`)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'contest_in_progress' && !force) {
        try {
          await ElMessageBox.confirm('场次未结束，将评测当前已回收的代码。', '仍要评测？', {
            type: 'warning',
            confirmButtonText: '仍要评测',
            cancelButtonText: '取消',
          })
        } catch {
          return
        }
        await judgeAll(true)
        return
      }
      ElMessage.error((err as Error).message)
    }
  }

  const actions: QuickAction[] = [
    { key: 'announce', label: '召集机器', icon: 'Promotion', group: '机器', run: announce },
    { key: 'bind', label: '按码配对', icon: 'Link', group: '机器', run: () => go('machines', 'bind') },
    {
      key: 'install',
      label: '装机命令',
      icon: 'Download',
      group: '机器',
      run: () => {
        window.open('/install', '_blank', 'noopener')
      },
    },
    { key: 'contest', label: '新建场次', icon: 'Plus', group: '考务', run: () => go('contests', 'new') },
    {
      key: 'deploy',
      label: '下发文件',
      icon: 'Upload',
      group: '代码与评测',
      needsContest: true,
      run: () => go('deploys', 'new'),
    },
    {
      key: 'judge',
      label: '评测全部题目',
      icon: 'Cpu',
      group: '代码与评测',
      needsContest: true,
      run: () => judgeAll(),
    },
    {
      key: 'rescan',
      label: '重扫成绩',
      icon: 'Refresh',
      group: '代码与评测',
      needsContest: true,
      run: rescan,
    },
    {
      key: 'export',
      label: '导出成绩 CSV',
      icon: 'Tickets',
      group: '代码与评测',
      needsContest: true,
      run: () => go('scores', 'export'),
    },
  ]

  return { actions, go }
}
