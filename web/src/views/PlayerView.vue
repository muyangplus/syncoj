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
      // 服务端的 `detail` 已经是一句能直接给选手看的中文（`docs/api-conventions.md` §3）
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
    fallbackError.value = '场次和考号都要填。'
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
 * 开考 / 结束时间在页面上怎么写。格式与「场次管理」列表**完全一致**
 * （`MM-DD HH:mm`，同机时区）：教师在那一页填的、选手在这里看的必须是同一个钟点。
 * 两处各写一种格式时，"到底是不是九点"这种问题会在考场里被问出来。
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
  context.value?.matched_by === 'machine' ? '（按本机自动识别的）' : '',
)

/** 失败时给下一步动作，而不是只把错误原样摊开。 */
const failureHint = computed(() => {
  if (!failure.value) return ''
  if (!failure.value.machine) {
    return '网络或服务端一时不通，点「重试」；一直不行就找监考老师。'
  }
  // **不要在兜底提示里替服务端下诊断。** 这里原来是"这台机器还没匹配到你的场次"，
  // 而上面那条告警可能是"还没注册上来"或"没配对"——同一台机器被说了两遍、还说岔了。
  // 具体原因由服务端的 detail 说（它才知道），这里只给下一步动作。
  return failure.value.auto
    ? '这件事得监考老师处理（原因见上面那句话）；也可以用下面的兜底方式自己查。'
    : '核对场次和考号；还是找不到就问监考老师。'
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
        <div>
          <h1 class="page-heading">考生须知</h1>
        </div>
        <!--
          「重新检测」放右上角、做成文字按钮：它是给"Agent 刚起来 / 老师刚配对完"
          这类时序问题用的补充动作，不是选手的第一选择，所以不该抢视线。
        -->
        <el-button link size="small" :loading="loading" @click="detect">重新检测</el-button>
      </header>

      <div class="result-area" :class="{ 'result-loading': loading }" v-loading="loading">
        <!-- 失败：服务端那句话 + 下一步该找谁 -->
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
            <!--
              重试只挂给网络 / 服务端故障：机器配不上时选手按多少次都没用，
              那种情况的主按钮是下面那张兜底表单里的「查看」。
            -->
            <el-button v-if="!failure.machine" link type="primary" size="small" @click="retry">
              重试
            </el-button>
          </div>
        </el-alert>

        <!--
          兜底表单：只在"服务端认不出这台机器"时露面。
          它是给教师的笔记本、或 Agent 没起来的机器留的一条路 —— 正常考场机器
          上选手永远看不到它，所以成功时页面里没有任何输入框。
        -->
        <el-card v-if="fallbackOpen" shadow="never" class="block">
          <h2 class="block-title">手动查（兜底）</h2>
          <p class="page-hint">这台机器自动识别不出来时用：填上你的场次和考号，点「查看」。</p>
          <div class="query-row">
            <el-input
              v-model="fallback.contest"
              class="query-input"
              size="large"
              placeholder="例如 2025-mock"
              clearable
              @keyup.enter="submitFallback"
            >
              <template #prepend>场次</template>
            </el-input>
            <el-input
              v-model="fallback.player_no"
              class="query-input mono-input"
              size="large"
              placeholder="例如 S001"
              clearable
              @keyup.enter="submitFallback"
            >
              <template #prepend>考号</template>
            </el-input>
            <el-button type="primary" size="large" :loading="loading" @click="submitFallback">
              查看
            </el-button>
          </div>
          <p v-if="fallbackError" class="form-error">{{ fallbackError }}</p>
        </el-card>

        <template v-if="context">
          <!--
            顺序按"先知道自己在哪一场，再看老师说了什么"排：场次卡（标题、状态、
            时间窗）在最上面，**考场公告紧跟其后**，然后是下发的文件。

            公告放在场次标题**下面**而不是页面最顶上：公告是"这一场"的话，先有场次
            再有公告才读得通；反过来会出现"到这一页先看到一段话，却不知道是哪场考试"
            的观感。

            正文按 **Markdown 渲染**（服务端给的是原文）：教师一定会用 `##`、`-`、
            `**` 这些写法，原样显示出来就是一堆符号。渲染之前一律过消毒，见
            `@/utils/markdown`。没有下发公告时整块不出现 —— 不留一个空框占地方。
          -->
          <el-card shadow="never" class="block">
            <div class="ident-row">
              <div class="ident-main">
                <span class="ident-name">{{ context.contest.name }}</span>
                <el-tag
                  :type="contestStatusType(context.contest.status)"
                  size="small"
                  effect="plain"
                >
                  {{ contestStatusLabel(context.contest.status) }}
                </el-tag>
                <span v-if="matchedLabel" class="muted ident-source">{{ matchedLabel }}</span>
              </div>
              <div class="ident-items">
                <span>考号 <strong class="mono">{{ context.player.player_no }}</strong></span>
                <span>姓名 <strong>{{ context.player.name || '—' }}</strong></span>
                <span>座位 <strong>{{ context.player.seat || '—' }}</strong></span>
                <span v-if="context.player.group_name">
                  分组 <strong>{{ context.player.group_name }}</strong>
                </span>
              </div>
            </div>
            <p v-if="hasWindow" class="page-hint">
              开考 <strong class="mono">{{ windowText(context.contest.starts_at) }}</strong>
              · 结束 <strong class="mono">{{ windowText(context.contest.ends_at) }}</strong>
            </p>
            <p v-if="serverClock" class="page-hint">
              服务端时间 <strong class="mono">{{ serverClock }}</strong><template v-if="clockSkew"> · {{ clockSkew }}</template>
            </p>
          </el-card>

          <!-- 考场公告：紧跟场次卡，见上面那段注释 -->
          <el-card v-if="notice" shadow="never" class="block">
            <div class="notice-head">
              <h2 class="block-title">考场公告</h2>
              <span class="muted">来自 {{ notice.filename }}</span>
            </div>
            <div class="notice-text md" v-html="noticeHtml"></div>
          </el-card>

          <el-card shadow="never" class="block">
            <div class="assets-head">
              <h2 class="block-title">下发给你的文件</h2>
              <span class="muted">{{ assets.length }} 个</span>
            </div>

            <el-table v-if="assets.length" :data="assets" size="small" border stripe>
              <el-table-column label="文件名" min-width="220">
                <template #default="{ row }">
                  <!--
                    sha256 不单独占一列：只有核对完整性时才用得上，而它太宽，
                    占一列会把文件名和落点挤到看不见。挂在文件名的悬浮提示里。
                  -->
                  <el-tooltip placement="top" popper-class="player-hash-popper">
                    <template #content>
                      <div class="hash-label">SHA256（核对完整性用）</div>
                      <div class="hash-value">{{ row.sha256 }}</div>
                    </template>
                    <span class="file-name">{{ row.filename }}</span>
                  </el-tooltip>
                </template>
              </el-table-column>

              <el-table-column label="放到哪" min-width="200">
                <template #default="{ row }">
                  <span v-if="row.dest_dir" class="mono dir">{{ row.dest_dir }}</span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>

              <el-table-column label="大小" width="90" align="right">
                <template #default="{ row }">{{ formatBytes(row.size) }}</template>
              </el-table-column>

              <el-table-column label="状态" width="90" align="center">
                <template #default="{ row }">
                  <el-tag :type="assetState(row.status).type" size="small" effect="plain">
                    {{ assetState(row.status).label }}
                  </el-tag>
                </template>
              </el-table-column>

              <el-table-column label="完成时间" width="160">
                <template #default="{ row }">
                  <span class="cell-sub">{{ formatTime(row.finished_at) }}</span>
                </template>
              </el-table-column>
            </el-table>

            <!--
              空表最容易出的问题是"它看起来像坏了"：选手不知道该等还是该问。
              所以这里明确说"就是没有下发"，而不是留一个空表格。
            -->
            <el-empty
              v-else
              :image-size="72"
              description="这场还没有给你下发任何文件"
            />
          </el-card>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.player-page {
  min-height: 100%;
  padding: 24px 16px 48px;
  background: var(--syncoj-bg);
}

.player-container {
  max-width: 1000px;
  margin: 0 auto;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.page-heading {
  margin: 0;
  font-size: 20px;
}

.page-heading-sub {
  margin: 6px 0 0;
  font-size: 13px;
  color: #606266;
}

/* 考场机器常见 1366×768：两个框加按钮放不下时换行，而不是把按钮挤出去 */
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

/* 考号要跟准考证逐个字符核，等宽字体下更不容易看错 */
.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  letter-spacing: 1px;
}

.result-area {
  position: relative;
}

/* 加载遮罩要有落脚点：结果区为空时元素高度为 0，转圈会看不见 */
.result-loading {
  min-height: 80px;
}

.block {
  margin-top: 14px;
}

.alert-body {
  line-height: 1.7;
}

.notice-text {
  font-size: 15px;
  line-height: 1.8;
  /* 老师多半是一行一条地写，保留换行，别压成一整段 */
  white-space: pre-wrap;
}

/*
 * 渲染出来的 Markdown 段落。
 *
 * 只补 element-plus 不提供的那些：标题要**比正文明显但不抢**、列表要有缩进、
 * 行内代码与代码块要一眼看出是代码。刻意不引一份 markdown 主题 —— 那会连
 * 正文的字体、行距、颜色一起改掉，与这一页其余部分对不上。
 */
.notice-text.md {
  /* 渲染成 HTML 之后由元素自己管换行，再 pre-wrap 会把标签间的空白也显示出来 */
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
  border-radius: 3px;
  background: var(--el-fill-color-light);
  font-family: var(--syncoj-mono, monospace);
  font-size: 0.92em;
}

.notice-text.md :deep(pre) {
  margin: 8px 0;
  padding: 8px 10px;
  overflow-x: auto;
  border-radius: 4px;
  background: var(--el-fill-color-light);
}

.notice-text.md :deep(pre code) {
  padding: 0;
  background: none;
}

.notice-text.md :deep(blockquote) {
  margin: 8px 0;
  padding-left: 10px;
  border-left: 3px solid var(--el-border-color);
  color: var(--el-text-color-secondary);
}

.notice-text.md :deep(table) {
  margin: 8px 0;
  border-collapse: collapse;
}

.notice-text.md :deep(th),
.notice-text.md :deep(td) {
  padding: 4px 10px;
  border: 1px solid var(--el-border-color-lighter);
}

.notice-text.md :deep(hr) {
  margin: 12px 0;
  border: none;
  border-top: 1px solid var(--el-border-color-lighter);
}

.notice-text.md :deep(a) {
  color: var(--el-color-primary);
}

/* 公告里贴一张截图（教师常用）时别把版面撑破 */
.notice-text.md :deep(img) {
  max-width: 100%;
}

.ident-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px 20px;
}

.ident-name {
  font-size: 16px;
  font-weight: 600;
  margin-right: 8px;
}

.ident-source {
  font-size: 12px;
  margin-left: 8px;
}

.ident-items {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  font-size: 13px;
  color: #606266;
}

.ident-items strong {
  font-weight: 600;
  color: #303133;
}

.block-title {
  margin: 0;
  font-size: 15px;
}

.assets-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 10px;
}

/* 公告标题行：标题 + 来源小字，和文件清单的标题行同一种排法 */
.notice-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 10px;
}

.form-error {
  margin: 10px 0 0;
  font-size: 13px;
  color: #f56c6c;
}

/* 长文件名/长路径换行显示，而不是把表格撑宽到需要横向滚动 */
.file-name {
  word-break: break-all;
  border-bottom: 1px dashed #c0c4cc;
  cursor: help;
}

.dir {
  word-break: break-all;
}
</style>

<!--
  el-tooltip 的浮层被 teleport 到 body 下，scoped 样式（带 data-v 属性选择器）够不着它，
  所以这条必须是全局的。类名带 `player-` 前缀，避免影响别的页面。
-->
<style>
.player-hash-popper {
  max-width: 340px;
}

.player-hash-popper .hash-label {
  font-size: 12px;
  color: #c0c4cc;
  margin-bottom: 4px;
}

.player-hash-popper .hash-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  /* 64 位十六进制中间没有空格，不强制断行就会撑破浮层 */
  word-break: break-all;
}
</style>
