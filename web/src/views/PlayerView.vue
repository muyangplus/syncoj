<script setup lang="ts">
/**
 * 选手页（**免登录**）—— 选手在考场机器上打开的一页。
 *
 * 正常情况下选手**什么都不用填**：进页面就自动按**来源 IP** 匹配 ——
 * 服务端认这台机器配对到了谁，再看那个人在哪个场次，直接给出他的清单。
 * 所以成功时页面上没有输入框；只有自动匹配不上（机器没注册 / 没配对 /
 * 没有含它的场次 / 同时在多个场次里）时，才退到"场次 + 考号"的兜底表单，
 * 给教师的笔记本、Agent 还没来得及起来的机器用。
 *
 * 免登录的页面没有 `AppLayout` 兜底，所以这里自己起一层浅色容器与最大宽度：
 * 管理端的侧边栏、导航都不该出现在选手眼前。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import { playerPageApi } from '@/api'
import { ApiError } from '@/api/client'
import { describeError, isAbort } from '@/api/crud'
import type { PlayerContextOut } from '@/api/playerPage'
import {
  contestStatusLabel,
  contestStatusType,
  formatBytes,
  formatTime,
} from '@/utils/format'
import { renderMarkdown } from '@/utils/markdown'

/**
 * 服务端"认不出这台机器 / 这个人"的状态码：没注册、没配对、没有含它的场次、
 * 或同时在多个场次里。它们的共同点是**选手自己重试没用**，得老师处理 ——
 * 所以要和网络故障分开，给的动作也不一样。
 */
const MATCH_FAILURE_STATUS: ReadonlySet<number> = new Set([403, 404, 409])

/** 查到的整份清单。null 表示手上还没有清单：首次自动匹配还没回来，或者匹配失败。 */
const context = ref<PlayerContextOut | null>(null)
const loading = ref(false)

/**
 * 上一次请求的失败。
 *
 * `machine` 区分"服务端不认得你"（要老师处理）与"请求没到达 / 服务端出错"
 * （可以重试）；`auto` 记住失败的是自动匹配还是兜底表单 —— 重试要重发**同一种**
 * 请求，提示的话也不一样（自动匹配失败不该让选手去核对考号）。
 */
const failure = ref<{ message: string; machine: boolean; auto: boolean } | null>(null)

/** 兜底表单。刻意空着，不从 URL 读任何东西：这是选手自己填的备选路径。 */
const fallback = reactive({ contest: '', player_no: '' })
const fallbackError = ref('')
/**
 * 兜底表单要不要露面。
 *
 * 用独立的状态而不是"`failure.machine` 就显示"：请求一开始会清掉 `failure`，
 * 直接看它的话，选手点了「查看」之后表单会先消失再出现 —— 而他正盯着自己刚填的
 * 考号。收起来的时机只有一个：拿到清单。
 */
const fallbackOpen = ref(false)

/** 一次请求的两种来源：自动匹配（不带参数）或兜底表单（带场次与考号）。 */
type Attempt = { auto: true } | { auto: false; contest: string; player_no: string }
let lastWasAuto = true

let inflight: AbortController | null = null

async function load(attempt: Attempt): Promise<void> {
  // 先取消上一轮：慢的那次回来不能把新结果盖掉
  inflight?.abort()
  inflight = null
  lastWasAuto = attempt.auto

  const controller = new AbortController()
  inflight = controller
  loading.value = true
  failure.value = null

  // 自动匹配**不带任何查询参数**：服务端按来源 IP 认机器。
  // 所以这里就是字面上的 `{}` —— 接口签名里两个参数都是可选的（服务端支持
  // "一个都不给"），不需要任何类型断言。
  const params = attempt.auto
    ? {}
    : { contest: attempt.contest, player_no: attempt.player_no }

  try {
    const result = await playerPageApi.context(params, controller.signal)
    if (controller.signal.aborted) return
    context.value = result
    // 记下"服务端时间 − 本机时间"这个差，之后靠它每秒走字（见 serverNow）。
    // 必须在**收到响应的这一刻**算：晚算一秒就多错一秒，而这一页要显示的恰恰是
    // 两台机器的钟差。
    if (result.server_time) {
      clockOffsetSeconds.value = result.server_time - Math.floor(Date.now() / 1000)
    }
    fallbackOpen.value = false
  } catch (error) {
    // 被自己发的新请求取消不是错误，静默丢弃；否则界面上会闪一条假报错
    if (isAbort(error)) return
    const machine = error instanceof ApiError && MATCH_FAILURE_STATUS.has(error.status)
    failure.value = {
      // 服务端的 `detail` 已经是一句能直接给选手看的中文（`docs/reference/api-conventions.md` §3）
      message: describeError(error),
      machine,
      auto: attempt.auto,
    }
    context.value = null
    // 机器匹配不上才给兜底表单；网络故障时让选手填场次考号也解决不了问题
    if (machine) fallbackOpen.value = true
  } finally {
    // 被取消说明有更新的请求在跑，它会把 loading 收掉，这里别抢着关
    if (!controller.signal.aborted) loading.value = false
    if (inflight === controller) inflight = null
  }
}

/** 重新跑一次自动匹配。成功页右上角那个不起眼的「重新检测」用的就是它。 */
function detect(): void {
  void load({ auto: true })
}

/**
 * 兜底表单提交。
 *
 * 空着发请求只会换来一句服务端的 422，所以在本地先拦住，把话说给选手听。
 */
function submitFallback(): void {
  const contest = fallback.contest.trim()
  const playerNo = fallback.player_no.trim()
  if (!contest || !playerNo) {
    fallbackError.value = '请填写场次与考号'
    return
  }
  fallbackError.value = ''
  void load({ auto: false, contest, player_no: playerNo })
}

/** 网络 / 服务端故障时重发一次；自动匹配失败过就再自动匹配，兜底表单则用当前填的值。 */
function retry(): void {
  if (lastWasAuto) {
    detect()
    return
  }
  // 走兜底表单的重试读的是**选手现在填的值** —— 他很可能刚把考号改对
  submitFallback()
}

onMounted(detect)

onBeforeUnmount(() => {
  inflight?.abort()
})

const assets = computed(() => context.value?.assets ?? [])
/**
 * 考场公告。`notice` 是**下发过来的资产**（文件名叫 NOTICE.md 的那一个），
 * 不是一个场次配置：没下发就是 null，整块都不渲染。
 *
 * 另外要求正文非空才显示：教师误发了一个空的 NOTICE.md 时，页面上不该出现一个
 * 只有标题、里面什么都没有的框 —— 那看起来像页面坏了。
 */
const notice = computed(() => {
  const value = context.value?.notice
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
 * 为什么不直接用返回来的 `server_time`：那是一个**快照**，页面开着的时候它不走。
 * 而考场上学生真正要看的是"现在几点"（离交卷还有多久、这道题还能写几分钟），
 * 一台时钟不准的机器上，这个数字比机器右下角那个可靠。
 *
 * 做法：拿到响应的那一刻记下"服务端时间 与 本机时间 的差"，之后就用本机时钟往前
 * 走 —— 不为了走字每秒发一次请求，而且这样显示出来的差**恰好就是两台机器的时钟差**，
 * 学生一眼能看出"我这台机器慢了 8 分钟"。
 *
 * 这里刻意不做任何"自动纠正本机时钟"的事：改系统时钟要 root、会牵连别的程序，
 * 而且那是装机时该做的事（NTP 或镜像里预置）。这一页只负责**如实显示**。
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
 * 本机时钟与服务端差多少。差的绝对值小于 1 分钟就不提 —— 秒级抖动是正常的，
 * 而提一句"差 8 秒"只会让人去调一个本来没问题的钟。
 *
 * 差值就是那个 offset 本身：本机和服务端的钟以同样的速度走，所以"差多少"在一次
 * 会话里是个常量，不需要每秒重算。`offset > 0` 表示**服务端在前面** → 本机慢了。
 */
const clockSkew = computed(() => {
  const offset = clockOffsetSeconds.value
  if (offset === null || Math.abs(offset) < 60) return ''
  const minutes = Math.round(Math.abs(offset) / 60)
  return offset > 0
    ? `本机时钟慢了约 ${minutes} 分钟`
    : `本机时钟快了约 ${minutes} 分钟`
})

/**
 * 这一场配了考试时间窗吗（任意一端配了就算配了）。
 *
 * 两端都没配时整段不显示 —— 那表示"这场没有时间限制"，页面上再写一句
 * "不限 → 不限"只是噪声。
 */
const hasWindow = computed(() => {
  const contest = context.value?.contest
  return Boolean(contest?.starts_at || contest?.ends_at)
})

/**
 * 开考 / 结束倒计时。**基准是服务端时间**（`serverNow`），不是本机时钟 ——
 * 选手把系统时钟往前拨两小时不该让"距结束"变短。
 *
 * 三态按场次时间窗判断，只显示其中一个：
 *   ``距开考 hh:mm:ss`` / ``距结束 hh:mm:ss`` / ``已结束``
 *
 * 每秒重算（`now` 每秒变一次，`serverNow` 跟着它走），所以页面上的秒数是活的。
 */
const countdown = computed<{ label: string; text: string } | null>(() => {
  const contest = context.value?.contest
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
 * （`MM-DD HH:mm`，同一个显示时区）：教师在那一页填的、选手在这里看的必须是
 * 同一个钟点。两处各写一种格式时，"到底是不是九点"这种问题会在考场里被问出来。
 *
 * 没配的那一端写「不限」，与管理界面同一套说法（不写"留空"—— 选手看不到那个表单）。
 */
function windowText(iso: string | null | undefined): string {
  return iso ? formatTime(iso).slice(5, 16) : '不限'
}

/**
 * `matched_by` 只用来在身份卡上补一句"清单是怎么来的"，不为它改动页面结构：
 * 选手关心的不是匹配方式，而是"这确实是我"。手动查询时不必特意说明。
 */
const matchedLabel = computed(() =>
  context.value?.matched_by === 'machine' ? '本机自动识别' : '',
)

/** 失败时给下一步动作，而不是只把错误原样摊开。 */
const failureHint = computed(() => {
  if (!failure.value) return ''
  if (!failure.value.machine) {
    return '重试无效请举手联系监考老师。'
  }
  // **不要在兜底提示里替服务端下诊断。** 这里原来是"这台机器还没匹配到你的场次"，
  // 而上面那条告警可能是"还没注册上来"或"没配对"——同一台机器被说了两遍、还说岔了。
  // 具体原因由服务端的 detail 说（它才知道），这里只给下一步动作。
  return failure.value.auto
    ? '请举手联系监考老师，或在下方手动查询。'
    : '请核对场次与考号。'
})

/**
 * `assets[].status` 是服务端的 `DeployStatus`，这里换成选手一眼能懂的说法。
 *
 * `pending` 与 `ready` 合成"等待中"：对选手来说没有区别，都是"还没到你手上"。
 * 认不出来的状态原样显示 —— 服务端将来加了新状态时，显示一个英文词也比
 * 硬塞进"等待中"然后让人以为传输正常要好。
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
        <el-button text :loading="loading" @click="detect">
          <el-icon><Refresh /></el-icon>重新检测
        </el-button>
      </header>

      <div class="result-area" :class="{ 'result-loading': loading }" v-loading="loading">
        <!-- 失败：服务端原话 + 下一步动作；重试只给网络 / 服务端故障 -->
        <el-alert
          v-if="failure"
          :type="failure.machine ? 'warning' : 'error'"
          :closable="false"
          show-icon
          class="block"
        >
          <template #title>{{ failure.message }}</template>
          <div class="alert-body">
            {{ failureHint }}
            <el-button v-if="!failure.machine" link type="primary" @click="retry">重试</el-button>
          </div>
        </el-alert>

        <!-- 兜底表单：只在服务端认不出这台机器时出现 -->
        <section v-if="fallbackOpen" class="panel block">
          <h2 class="block-title">手动查询</h2>
          <div class="query-row">
            <el-input
              v-model="fallback.contest"
              class="query-input"
              placeholder="例如 2025-mock"
              clearable
              @keyup.enter="submitFallback"
            >
              <template #prepend>场次</template>
            </el-input>
            <el-input
              v-model="fallback.player_no"
              class="query-input mono-input"
              placeholder="例如 S001"
              clearable
              @keyup.enter="submitFallback"
            >
              <template #prepend>考号</template>
            </el-input>
            <el-button type="primary" :loading="loading" @click="submitFallback">
              <el-icon><Search /></el-icon>查看
            </el-button>
          </div>
          <p v-if="fallbackError" class="form-error">{{ fallbackError }}</p>
        </section>

        <template v-if="context">
          <!-- 顺序：场次卡 → 考场公告 → 下发文件 -->
          <section class="panel block contest-card">
            <div class="contest-main">
              <div class="contest-title-row">
                <span class="ident-name">{{ context.contest.name }}</span>
                <el-tag :type="contestStatusType(context.contest.status)" effect="light">
                  {{ contestStatusLabel(context.contest.status) }}
                </el-tag>
                <el-tag v-if="matchedLabel" type="info" effect="plain" size="small">{{ matchedLabel }}</el-tag>
              </div>

              <dl class="ident-items">
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
                  开考 <strong class="mono">{{ windowText(context.contest.starts_at) }}</strong>
                  · 结束 <strong class="mono">{{ windowText(context.contest.ends_at) }}</strong>
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

          <section class="panel block panel-flush">
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

              <el-table-column label="存放位置" min-width="200">
                <template #default="{ row }">
                  <span v-if="row.dest_dir" class="mono dir">{{ row.dest_dir }}</span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>

              <el-table-column label="大小" width="100" align="right">
                <template #default="{ row }">{{ formatBytes(row.size) }}</template>
              </el-table-column>

              <el-table-column label="状态" width="100" align="center">
                <template #default="{ row }">
                  <el-tag :type="assetState(row.status).type" size="small" effect="light">
                    {{ assetState(row.status).label }}
                  </el-tag>
                </template>
              </el-table-column>

              <el-table-column label="完成时间" width="170">
                <template #default="{ row }">
                  <span class="cell-sub">{{ formatTime(row.finished_at) }}</span>
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
  gap: 8px;
  margin-bottom: 12px;
}

.panel-head.padded {
  margin: 0;
  padding: 14px 20px;
  border-bottom: 1px solid var(--sj-border);
}

.block-title {
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
