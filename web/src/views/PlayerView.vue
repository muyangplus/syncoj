<script setup lang="ts">
/**
 * 考生页（**不要管理端凭据**）—— 选手在考场机器上打开的一页。
 *
 * 三种状态，进页面按顺序试：
 *
 * 1. sessionStorage 里有登录令牌 → 以那位考生的身份取清单（令牌失效就丢掉、往下走）
 * 2. 按**来源 IP** 自动匹配本机 → 选手什么都不用填，直接看到自己的清单
 * 3. 都不行 → **降级预览**：当前在考的场次（多场可切换）、时间与倒计时、全场公告；
 *    场次设了考生页密码时多一个「以选手身份查看」表单（准考证号 + 密码）
 *
 * 降级预览里**没有任何个人信息**——那是服务端的边界（`/player/preview` 不返回），
 * 不是这一页"不显示"。
 *
 * 免登录的页面没有 `AppLayout` 兜底，所以这里自己起一层浅色容器与最大宽度：
 * 管理端的侧边栏、导航都不该出现在选手眼前。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { playerPageApi } from '@/api'
import { ApiError } from '@/api/client'
import { describeError, isAbort } from '@/api/crud'
import type {
  PlayerAssetOut,
  PlayerContextOut,
  PlayerPreviewContestOut,
  PlayerPreviewOut,
} from '@/api/playerPage'
import {
  contestStatusLabel,
  contestStatusType,
  formatBytes,
  formatTime,
} from '@/utils/format'
import { renderMarkdown } from '@/utils/markdown'

/** 登录令牌放 sessionStorage：关掉标签页就忘，考场机器上不留到下一个人。 */
const TOKEN_KEY = 'syncoj.player.token'

function readToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

function writeToken(value: string | null): void {
  try {
    if (value) window.sessionStorage.setItem(TOKEN_KEY, value)
    else window.sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 隐私模式下 sessionStorage 可能抛异常：退化成"这一页内有效" */
  }
}

/**
 * 服务端"认不出这台机器"的状态码：没注册、没配对、没有含它的场次、
 * 或同时在多个场次里。它们都退到降级预览，而不是一条报错。
 */
const MATCH_FAILURE_STATUS: ReadonlySet<number> = new Set([403, 404, 409])

const token = ref<string | null>(readToken())
/** 个人上下文（本机匹配或登录）。null = 现在显示的是降级预览（或还在加载）。 */
const context = ref<PlayerContextOut | null>(null)
const preview = ref<PlayerPreviewOut | null>(null)
/** 本机自动匹配失败时服务端的原话，降级预览里作为一条状态显示。 */
const machineNote = ref('')
const selectedSlug = ref('')
const loading = ref(false)
/** 网络 / 服务端故障（不是"认不出你"）。 */
const failure = ref('')

let inflight: AbortController | null = null

function clockFrom(serverTime: number | undefined): void {
  // 记下"服务端时间 − 本机时间"这个差，之后靠它每秒走字（见 serverNow）。
  // 必须在**收到响应的这一刻**算：晚算一秒就多错一秒。
  if (serverTime) {
    clockOffsetSeconds.value = serverTime - Math.floor(Date.now() / 1000)
  }
}

async function load(): Promise<void> {
  // 先取消上一轮：慢的那次回来不能把新结果盖掉
  inflight?.abort()
  const controller = new AbortController()
  inflight = controller
  loading.value = true
  failure.value = ''

  try {
    if (token.value) {
      try {
        const result = await playerPageApi.context(token.value, controller.signal)
        if (controller.signal.aborted) return
        showPersonal(result)
        return
      } catch (error) {
        if (isAbort(error)) return
        if (!(error instanceof ApiError && error.code === 'player_token_invalid')) throw error
        // 令牌过期 / 密码被换掉：丢掉它，按没登录的流程往下走
        token.value = null
        writeToken(null)
      }
    }

    try {
      const result = await playerPageApi.context(null, controller.signal)
      if (controller.signal.aborted) return
      showPersonal(result)
      return
    } catch (error) {
      if (isAbort(error)) return
      if (!(error instanceof ApiError && MATCH_FAILURE_STATUS.has(error.status))) throw error
      machineNote.value = error.message
    }

    const result = await playerPageApi.preview(controller.signal)
    if (controller.signal.aborted) return
    context.value = null
    preview.value = result
    clockFrom(result.server_time)
    const slugs = (result.contests ?? []).map((item) => item.slug)
    if (!slugs.includes(selectedSlug.value)) selectedSlug.value = slugs[0] ?? ''
  } catch (error) {
    if (isAbort(error)) return
    failure.value = describeError(error)
  } finally {
    // 被取消说明有更新的请求在跑，它会把 loading 收掉，这里别抢着关
    if (!controller.signal.aborted) loading.value = false
    if (inflight === controller) inflight = null
  }
}

function showPersonal(result: PlayerContextOut): void {
  context.value = result
  machineNote.value = ''
  clockFrom(result.server_time)
}

onMounted(() => void load())

onBeforeUnmount(() => {
  inflight?.abort()
})

// ---- 降级预览：场次切换 ----

const previewContests = computed(() => preview.value?.contests ?? [])
const previewContest = computed<PlayerPreviewContestOut | null>(
  () => previewContests.value.find((item) => item.slug === selectedSlug.value) ?? null,
)

// ---- 以选手身份查看 ----

const loginForm = reactive({ player_no: '', password: '' })
const loginError = ref('')
const loggingIn = ref(false)

// 换场次时清掉上一场的报错：那句话说的是另一场
watch(selectedSlug, () => {
  loginError.value = ''
})

async function submitLogin(): Promise<void> {
  const contest = previewContest.value
  if (!contest || loggingIn.value) return
  const playerNo = loginForm.player_no.trim()
  if (!playerNo || !loginForm.password) {
    loginError.value = '请填写准考证号与密码'
    return
  }
  loginError.value = ''
  loggingIn.value = true
  try {
    const result = await playerPageApi.login({
      contest: contest.slug,
      player_no: playerNo,
      password: loginForm.password,
    })
    token.value = result.token
    writeToken(result.token)
    loginForm.password = ''
    await load()
  } catch (error) {
    // 服务端的 `detail` 已经是一句能直接给选手看的中文
    loginError.value = describeError(error)
  } finally {
    loggingIn.value = false
  }
}

function logout(): void {
  token.value = null
  writeToken(null)
  context.value = null
  void load()
}

// ---- 下载 ----

const downloading = ref<number | null>(null)

async function downloadAsset(row: PlayerAssetOut): Promise<void> {
  if (downloading.value !== null) return
  downloading.value = row.id
  try {
    await playerPageApi.download(row.id, row.filename, token.value)
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    downloading.value = null
  }
}

// ---- 显示 ----

const assets = computed(() => context.value?.assets ?? [])

/** 当前卡片上的场次：个人上下文优先，否则是降级预览里选中的那一场。 */
const shownContest = computed(() => context.value?.contest ?? previewContest.value)

/**
 * 考场公告。个人上下文里是**发给本人**的 `NOTICE.md`；降级预览里只有**发给全场**的。
 *
 * 另外要求正文非空才显示：教师误发了一个空的 NOTICE.md 时，页面上不该出现一个
 * 只有标题、里面什么都没有的框 —— 那看起来像页面坏了。
 */
const notice = computed(() => {
  const value = context.value ? context.value.notice : previewContest.value?.notice
  return value && value.content.trim() ? value : null
})

/**
 * 公告正文渲染成 HTML（Markdown → 消毒后的 HTML）。
 *
 * 消毒那一步在 `@/utils/markdown` 里做，**不能省**：这段 HTML 要交给 v-html，
 * 而公告的正文是教师从别处粘进来的。
 */
const noticeHtml = computed(() =>
  notice.value ? renderMarkdown(notice.value.content) : '',
)

/**
 * **服务端现在几点**，每秒自己走字。
 *
 * 拿到响应的那一刻记下"服务端时间 与 本机时间 的差"，之后就用本机时钟往前走 ——
 * 不为了走字每秒发一次请求，而且显示出来的差**恰好就是两台机器的时钟差**。
 * 这里刻意不做任何"自动纠正本机时钟"的事，只负责**如实显示**。
 */
const clockOffsetSeconds = ref<number | null>(null)
const now = ref(Date.now())

onMounted(() => {
  const timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
  onBeforeUnmount(() => window.clearInterval(timer))
})

/** 服务端当前时间（本地渲染用）。 */
const serverNow = computed(() => {
  if (clockOffsetSeconds.value === null) return null
  return new Date((clockOffsetSeconds.value + Math.floor(now.value / 1000)) * 1000)
})

const serverClock = computed(() => {
  const moment = serverNow.value
  if (!moment) return ''
  return moment.toLocaleTimeString('zh-CN', { hour12: false })
})

/**
 * 本机时钟与服务端差多少。差的绝对值小于 1 分钟就不提 —— 秒级抖动是正常的。
 * `offset > 0` 表示**服务端在前面** → 本机慢了。
 */
const clockSkew = computed(() => {
  const offset = clockOffsetSeconds.value
  if (offset === null || Math.abs(offset) < 60) return ''
  const minutes = Math.round(Math.abs(offset) / 60)
  return offset > 0
    ? `本机时钟慢了约 ${minutes} 分钟`
    : `本机时钟快了约 ${minutes} 分钟`
})

/** 这一场配了考试时间窗吗（任意一端配了就算配了）。两端都没配时整段不显示。 */
const hasWindow = computed(() => {
  const contest = shownContest.value
  return Boolean(contest?.starts_at || contest?.ends_at)
})

/**
 * 开考 / 结束倒计时。**基准是服务端时间**（`serverNow`），不是本机时钟 ——
 * 选手把系统时钟往前拨两小时不该让"距结束"变短。
 *
 * 三态：``距开考 hh:mm:ss`` / ``距结束 hh:mm:ss`` / ``已结束``
 */
const countdown = computed<{ label: string; text: string } | null>(() => {
  const contest = shownContest.value
  const moment = serverNow.value
  if (!contest || !moment) return null
  const nowSeconds = Math.floor(moment.getTime() / 1000)

  if (contest.starts_at) {
    const start = Math.floor(new Date(contest.starts_at).getTime() / 1000)
    if (nowSeconds < start) return { label: '距开考', text: formatDuration(start - nowSeconds) }
  }
  if (contest.ends_at) {
    const end = Math.floor(new Date(contest.ends_at).getTime() / 1000)
    if (nowSeconds >= end) return { label: '', text: '已结束' }
    return { label: '距结束', text: formatDuration(end - nowSeconds) }
  }
  // 只配了开考时间：开考之后没有"结束"可言，整段不显示
  return null
})

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`
}

/**
 * 开考 / 结束时间在页面上怎么写。格式与「场次管理」列表**完全一致**
 * （`MM-DD HH:mm`，同一个显示时区）。没配的那一端写「不限」。
 */
function windowText(iso: string | null | undefined): string {
  return iso ? formatTime(iso).slice(5, 16) : '不限'
}

/** 身份卡上补一句"清单是怎么来的"。 */
const matchedLabel = computed(() => {
  if (context.value?.matched_by === 'machine') return '本机自动识别'
  if (context.value?.matched_by === 'token') return '已登录'
  return ''
})

/**
 * `assets[].status` 是服务端的 `DeployStatus`，这里换成选手一眼能懂的说法。
 * `pending` 与 `ready` 合成"等待中"。认不出来的状态原样显示。
 */
function assetState(status: string): { label: string; type: 'success' | 'info' | 'danger' } {
  switch (status) {
    case 'done':
      return { label: '已收到', type: 'success' }
    case 'failed':
      return { label: '失败', type: 'danger' }
    case 'cancelled':
      // 灰色：取消是老师那边的动作，不是选手做错了什么
      return { label: '已取消', type: 'info' }
    case 'pending':
    case 'ready':
      return { label: '等待中', type: 'info' }
    default:
      return { label: status, type: 'info' }
  }
}
</script>

<template>
  <div class="player-page">
    <div class="player-container">
      <header class="page-head">
        <div class="brand">
          <span class="brand-mark">S</span>
          <h1 class="page-heading">考生须知</h1>
        </div>
        <div class="head-actions">
          <el-button v-if="context?.matched_by === 'token'" text @click="logout">
            <el-icon><SwitchButton /></el-icon>退出
          </el-button>
          <el-button text :loading="loading" @click="load">
            <el-icon><Refresh /></el-icon>刷新
          </el-button>
        </div>
      </header>

      <div class="result-area" :class="{ 'result-loading': loading }" v-loading="loading">
        <!-- 网络 / 服务端故障：服务端原话 + 重试 -->
        <el-alert v-if="failure" type="error" :closable="false" show-icon class="block">
          <template #title>{{ failure }}</template>
          <div class="alert-body">
            <el-button link type="primary" @click="load">重试</el-button>
          </div>
        </el-alert>

        <!-- 降级预览：场次切换 -->
        <template v-if="!context && preview">
          <div v-if="previewContests.length > 1" class="block contest-switch">
            <el-radio-group v-model="selectedSlug">
              <el-radio-button
                v-for="item in previewContests"
                :key="item.slug"
                :value="item.slug"
              >
                {{ item.name }}
              </el-radio-button>
            </el-radio-group>
          </div>

          <section v-if="!previewContests.length" class="panel block">
            <el-empty :image-size="72" description="当前没有进行中的场次" />
          </section>
        </template>

        <template v-if="shownContest">
          <!-- 顺序：场次卡 → 考场公告 → 下发文件 / 以选手身份查看 -->
          <section class="panel block contest-card">
            <div class="contest-main">
              <div class="contest-title-row">
                <span class="ident-name">{{ shownContest.name }}</span>
                <el-tag :type="contestStatusType(shownContest.status)" effect="light">
                  {{ contestStatusLabel(shownContest.status) }}
                </el-tag>
                <el-tag v-if="matchedLabel" type="info" effect="plain" size="small">{{ matchedLabel }}</el-tag>
              </div>

              <dl v-if="context" class="ident-items">
                <div>
                  <dt>考号</dt>
                  <dd class="mono">{{ context.player.player_no }}</dd>
                </div>
                <div>
                  <dt>姓名</dt>
                  <dd>{{ context.player.name || '—' }}</dd>
                </div>
                <div>
                  <dt>座位</dt>
                  <dd>{{ context.player.seat || '—' }}</dd>
                </div>
                <div v-if="context.player.group_name">
                  <dt>分组</dt>
                  <dd>{{ context.player.group_name }}</dd>
                </div>
              </dl>

              <div class="time-meta">
                <span v-if="hasWindow">
                  开考 <strong class="mono">{{ windowText(shownContest.starts_at) }}</strong>
                  · 结束 <strong class="mono">{{ windowText(shownContest.ends_at) }}</strong>
                </span>
                <span v-if="serverClock">
                  服务端时间 <strong class="mono">{{ serverClock }}</strong>
                </span>
                <span v-if="clockSkew" class="skew">{{ clockSkew }}</span>
              </div>
            </div>

            <!-- 倒计时：基准是服务端时间，改本机时钟无效 -->
            <div v-if="countdown" class="countdown" :class="{ ended: !countdown.label }">
              <span v-if="countdown.label" class="countdown-label">{{ countdown.label }}</span>
              <span class="countdown-value mono">{{ countdown.text }}</span>
            </div>
          </section>

          <section v-if="notice" class="panel block">
            <div class="panel-head">
              <h2 class="block-title">考场公告</h2>
              <span class="muted">{{ notice.filename }}</span>
            </div>
            <div class="notice-text md" v-html="noticeHtml"></div>
          </section>

          <!-- 以选手身份查看：只在本场开放了考生登录时出现 -->
          <section v-if="!context && previewContest?.login_enabled" class="panel block">
            <div class="panel-head">
              <h2 class="block-title">以选手身份查看</h2>
              <span v-if="machineNote" class="muted">{{ machineNote }}</span>
            </div>
            <div class="query-row">
              <el-input
                v-model="loginForm.player_no"
                class="query-input mono-input"
                placeholder="例如 S001"
                autocomplete="off"
                clearable
                @keyup.enter="submitLogin"
              >
                <template #prepend>准考证号</template>
              </el-input>
              <el-input
                v-model="loginForm.password"
                class="query-input"
                type="password"
                show-password
                autocomplete="off"
                @keyup.enter="submitLogin"
              >
                <template #prepend>密码</template>
              </el-input>
              <el-button type="primary" :loading="loggingIn" @click="submitLogin">
                <el-icon><User /></el-icon>查看
              </el-button>
            </div>
            <p v-if="loginError" class="form-error">{{ loginError }}</p>
          </section>
          <p v-else-if="!context && machineNote" class="machine-note muted block">{{ machineNote }}</p>

          <section v-if="context" class="panel block panel-flush">
            <div class="panel-head padded">
              <h2 class="block-title">下发文件</h2>
              <span class="muted">{{ assets.length }} 个</span>
            </div>

            <el-table v-if="assets.length" :data="assets">
              <el-table-column label="文件名" min-width="220">
                <template #default="{ row }">
                  <!-- sha256 太宽不占列，挂在文件名的悬浮提示里 -->
                  <el-tooltip placement="top" popper-class="player-hash-popper">
                    <template #content>
                      <div class="hash-label">SHA256</div>
                      <div class="hash-value">{{ row.sha256 }}</div>
                    </template>
                    <span class="file-name">{{ row.filename }}</span>
                  </el-tooltip>
                </template>
              </el-table-column>

              <el-table-column label="存放位置" min-width="180">
                <template #default="{ row }">
                  <span v-if="row.dest_dir" class="mono dir">{{ row.dest_dir }}</span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>

              <el-table-column label="大小" width="96" align="right">
                <template #default="{ row }">{{ formatBytes(row.size) }}</template>
              </el-table-column>

              <el-table-column label="状态" width="96" align="center">
                <template #default="{ row }">
                  <el-tag :type="assetState(row.status).type" size="small" effect="light">
                    {{ assetState(row.status).label }}
                  </el-tag>
                </template>
              </el-table-column>

              <el-table-column label="完成时间" width="150">
                <template #default="{ row }">
                  <span class="cell-sub">{{ formatTime(row.finished_at) }}</span>
                </template>
              </el-table-column>

              <el-table-column label="" width="92" align="center">
                <template #default="{ row }">
                  <el-button
                    v-if="row.status !== 'cancelled'"
                    link
                    type="primary"
                    :loading="downloading === row.id"
                    @click="downloadAsset(row)"
                  >
                    <el-icon><Download /></el-icon>下载
                  </el-button>
                </template>
              </el-table-column>
            </el-table>

            <el-empty v-else :image-size="72" description="无下发文件" />
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.player-page {
  min-height: 100%;
  padding: 28px 16px 48px;
  background:
    radial-gradient(60% 30% at 50% 0%, var(--sj-primary-soft) 0%, transparent 70%),
    var(--sj-bg);
  color: var(--sj-text);
}

.player-container {
  max-width: 1000px;
  margin: 0 auto;
}

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 与 AppLayout 侧栏的品牌方块一致 */
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: none;
  border-radius: 10px;
  background: var(--sj-brand-gradient);
  color: var(--el-color-white);
  font-weight: 700;
  font-size: 17px;
}

.page-heading {
  margin: 0;
  font-size: 21px;
  font-weight: 700;
  letter-spacing: -0.01em;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.contest-switch {
  overflow-x: auto;
}

.machine-note {
  margin-bottom: 0;
  font-size: 13px;
}

.panel {
  padding: 18px 20px;
  background: var(--sj-surface);
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius);
  box-shadow: var(--sj-shadow);
}

.panel-flush {
  padding: 0;
  overflow: hidden;
}

.panel-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 8px;
  margin-bottom: 12px;
}

.panel-head.padded {
  margin: 0;
  padding: 14px 20px;
  border-bottom: 1px solid var(--sj-border);
}

.block-title {
  flex: none;
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

/* 考场机器常见 1366×768：放不下时换行，而不是把按钮挤出去 */
.query-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}

.query-input {
  flex: 1 1 220px;
  min-width: 200px;
  max-width: 320px;
}

/* 考号要跟准考证逐字核对，等宽更不易看错 */
.mono-input :deep(input) {
  font-family: var(--sj-mono);
  letter-spacing: 1px;
}

.result-area {
  position: relative;
}

/* 结果区为空时给加载遮罩一个落脚点 */
.result-loading {
  min-height: 80px;
}

.block {
  margin-top: 16px;
}

.alert-body {
  line-height: 1.7;
}

/* ---- 场次卡 ---- */

.contest-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px 24px;
  flex-wrap: wrap;
}

.contest-main {
  min-width: 0;
  flex: 1 1 420px;
}

.contest-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.ident-name {
  font-size: 18px;
  font-weight: 700;
}

.ident-items {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 28px;
  margin: 14px 0 0;
}

.ident-items dt {
  font-size: 12px;
  color: var(--sj-text-3);
}

.ident-items dd {
  margin: 2px 0 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--sj-text);
}

.time-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin-top: 14px;
  font-size: 13px;
  color: var(--sj-text-2);
}

.time-meta strong {
  color: var(--sj-text);
}

.skew {
  color: var(--sj-warning);
}

/* 倒计时：场上最需要一眼看到的数 */
.countdown {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-width: 200px;
  padding: 14px 22px;
  border-radius: var(--sj-radius);
  background: var(--sj-primary-soft);
  color: var(--sj-primary);
}

.countdown.ended {
  background: var(--sj-info-soft);
  color: var(--sj-text-2);
}

.countdown-label {
  font-size: 12.5px;
  font-weight: 600;
}

.countdown-value {
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 1px;
  line-height: 1.25;
}

/* ---- 公告（Markdown，已消毒） ---- */

.notice-text {
  font-size: 15px;
  line-height: 1.8;
  color: var(--sj-text);
  white-space: pre-wrap;
}

.notice-text.md {
  white-space: normal;
}

.notice-text.md :deep(h1),
.notice-text.md :deep(h2),
.notice-text.md :deep(h3),
.notice-text.md :deep(h4) {
  margin: 12px 0 6px;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.6;
}

.notice-text.md :deep(h1) {
  font-size: 17px;
}

.notice-text.md :deep(p) {
  margin: 6px 0;
}

.notice-text.md :deep(ul),
.notice-text.md :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}

.notice-text.md :deep(li) {
  margin: 2px 0;
}

.notice-text.md :deep(code) {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--sj-surface-2);
  font-family: var(--sj-mono);
  font-size: 0.92em;
}

.notice-text.md :deep(pre) {
  margin: 8px 0;
  padding: 10px 12px;
  overflow-x: auto;
  border-radius: var(--sj-radius-sm);
  background: var(--sj-surface-2);
  border: 1px solid var(--sj-border);
}

.notice-text.md :deep(pre code) {
  padding: 0;
  background: none;
}

.notice-text.md :deep(blockquote) {
  margin: 8px 0;
  padding-left: 10px;
  border-left: 3px solid var(--sj-border-strong);
  color: var(--sj-text-2);
}

.notice-text.md :deep(table) {
  margin: 8px 0;
  border-collapse: collapse;
}

.notice-text.md :deep(th),
.notice-text.md :deep(td) {
  padding: 4px 10px;
  border: 1px solid var(--sj-border);
}

.notice-text.md :deep(hr) {
  margin: 12px 0;
  border: none;
  border-top: 1px solid var(--sj-border);
}

.notice-text.md :deep(a) {
  color: var(--sj-primary);
}

.notice-text.md :deep(img) {
  max-width: 100%;
}

.form-error {
  margin: 10px 0 0;
  font-size: 13px;
  color: var(--sj-danger);
}

/* 长文件名/长路径换行，不撑出横向滚动 */
.file-name {
  word-break: break-all;
  border-bottom: 1px dashed var(--sj-border-strong);
  cursor: help;
}

.dir {
  word-break: break-all;
}
</style>

<!-- tooltip 浮层被 teleport 到 body，scoped 够不着，所以全局（带 player- 前缀） -->
<style>
.player-hash-popper {
  max-width: 340px;
}

.player-hash-popper .hash-label {
  font-size: 12px;
  opacity: 0.7;
  margin-bottom: 4px;
}

.player-hash-popper .hash-value {
  font-family: var(--sj-mono);
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
}
</style>
