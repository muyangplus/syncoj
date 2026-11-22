<script setup lang="ts">
/** 工作台：当前场次的关键数字、快捷操作、召集状态、最近事件。 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { authApi, eventApi, judgingApi, machineApi, scoreApi } from '@/api'
import type {
  AnnounceStatusOut,
  BuiltinJudgeProgressOut,
  EventOut,
  ScoreMatrixOut,
} from '@/api/types'
import PageShell from '@/components/PageShell.vue'
import SectionCard from '@/components/SectionCard.vue'
import StatCard from '@/components/StatCard.vue'
import { usePolling } from '@/composables/usePolling'
import { useQuickActions } from '@/composables/useQuickActions'
import { useContestStore } from '@/stores/contest'
import {
  contestStatusLabel,
  contestStatusType,
  eventCategoryLabel,
  formatSince,
  formatTime,
} from '@/utils/format'

const router = useRouter()
const contest = useContestStore()
const { actions } = useQuickActions()

const health = ref<{ online: number; total: number } | null>(null)
const pending = ref<number | null>(null)
const matrix = ref<ScoreMatrixOut | null>(null)
const judge = ref<BuiltinJudgeProgressOut | null>(null)
const events = ref<EventOut[]>([])
const announce = ref<AnnounceStatusOut | null>(null)
const error = ref<string | null>(null)
const now = ref(Date.now())

async function load(): Promise<void> {
  const contestId = contest.currentId
  const tasks: Promise<unknown>[] = [
    authApi.health().then((h) => (health.value = { online: h.agents_online, total: h.agents_total })),
    machineApi.pending({ limit: 1 }).then((page) => (pending.value = page.total)),
    machineApi.announceStatus().then((status) => (announce.value = status)),
  ]
  if (contestId) {
    tasks.push(
      scoreApi.matrix(contestId).then((value) => (matrix.value = value)),
      judgingApi.progress(contestId).then((value) => (judge.value = value)),
      eventApi.list(contestId, { limit: 8 }).then((page) => (events.value = page.items)),
    )
  } else {
    matrix.value = null
    judge.value = null
    events.value = []
  }
  const results = await Promise.allSettled(tasks)
  const failed = results.find((item) => item.status === 'rejected') as PromiseRejectedResult | undefined
  error.value = failed ? (failed.reason as Error).message : null
}

watch(() => contest.currentId, () => void load())
const polling = usePolling(load, { interval: 10000 })

const ticker = window.setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => window.clearInterval(ticker))

// ---- 派生数字 ----

const cells = computed(() => {
  const rows = matrix.value?.rows ?? []
  const declared = new Set((matrix.value?.columns ?? []).filter((c) => c.declared).map((c) => c.ident))
  let total = 0
  let submitted = 0
  let scored = 0
  for (const row of rows) {
    for (const cell of row.cells ?? []) {
      if (!declared.has(cell.problem)) continue
      total += 1
      if (cell.submitted || cell.parse_status !== 'missing') submitted += 1
      if (cell.parse_status === 'ok' || cell.parse_status === 'manual') scored += 1
    }
  }
  return { total, submitted, scored, players: rows.length, problems: declared.size }
})

function pct(part: number, whole: number): number | null {
  return whole ? Math.round((part / whole) * 100) : null
}

const judgePct = computed(() => {
  const p = judge.value
  return p && p.total ? pct(p.done + p.failed, p.total) : null
})

// ---- 场次时间 ----

const window_ = computed(() => {
  const c = contest.current
  if (!c) return null
  const start = c.starts_at ? new Date(c.starts_at).getTime() : null
  const end = c.ends_at ? new Date(c.ends_at).getTime() : null
  return { start, end }
})

function span(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

const clock = computed(() => {
  const w = window_.value
  if (!w) return null
  if (w.start && now.value < w.start) return { label: '距开考', value: span(w.start - now.value) }
  if (w.end && now.value < w.end) return { label: '距结束', value: span(w.end - now.value) }
  if (w.end) return { label: '已结束', value: formatTime(new Date(w.end).toISOString()) }
  return null
})

async function announceNow(): Promise<void> {
  await actions[0].run()
  await polling.refresh()
}

function since(iso: string): string {
  return formatSince((now.value - new Date(iso).getTime()) / 1000)
}

const ACTION_TONE: Record<string, string> = {
  机器: 'primary',
  考务: 'success',
  代码与评测: 'warning',
}

function eventTone(level: string): string {
  if (level === 'error') return 'danger'
  if (level === 'warning') return 'warning'
  return 'info'
}
</script>

<template>
  <PageShell title="工作台" :error="error" retryable @retry="polling.refresh">
    <template #sub>
      <div v-if="contest.current" class="contest-line">
        <span class="contest-name">{{ contest.current.name }}</span>
        <el-tag size="small" :type="contestStatusType(contest.current.status)">
          {{ contestStatusLabel(contest.current.status) }}
        </el-tag>
        <span class="mono muted">{{ contest.current.slug }}</span>
      </div>
    </template>
    <template #toolbar>
      <div v-if="clock" class="clock">
        <span class="clock-label">{{ clock.label }}</span>
        <span class="clock-value">{{ clock.value }}</span>
      </div>
    </template>

    <div class="stats">
      <StatCard
        label="在线机器"
        :value="health ? `${health.online} / ${health.total}` : '—'"
        sub="在线 / 已注册"
        icon="Monitor"
        tone="success"
        :progress="health ? pct(health.online, health.total) : null"
        @click="router.push({ name: 'overview' })"
      />
      <StatCard
        label="待配对机器"
        :value="pending ?? '—'"
        :sub="pending ? '点击配对' : '无待配对'"
        icon="Connection"
        :tone="pending ? 'warning' : 'info'"
        @click="router.push({ name: 'machines' })"
      />
      <StatCard
        label="已交代码"
        :value="contest.currentId ? `${cells.submitted} / ${cells.total}` : '—'"
        :sub="contest.currentId ? `${cells.players} 名选手 × ${cells.problems} 道题` : '未选场次'"
        icon="Document"
        tone="primary"
        :progress="pct(cells.submitted, cells.total)"
        @click="router.push({ name: 'files' })"
      />
      <StatCard
        label="已出成绩"
        :value="contest.currentId ? `${cells.scored} / ${cells.submitted}` : '—'"
        :sub="matrix?.unparsed ? `${matrix.unparsed} 格未解析` : '已出 / 已交'"
        icon="Trophy"
        :tone="matrix?.unparsed ? 'danger' : 'success'"
        :progress="pct(cells.scored, cells.submitted)"
        @click="router.push({ name: 'scores' })"
      />
      <StatCard
        label="自带评测"
        :value="judge && judge.total ? `${judge.done} / ${judge.total}` : '—'"
        :sub="
          judge && judge.total
            ? `排队 ${judge.queued} · 评测中 ${judge.running} · 失败 ${judge.failed}`
            : '无评测任务'
        "
        icon="Cpu"
        :tone="judge?.failed ? 'danger' : 'primary'"
        :progress="judgePct"
        @click="router.push({ name: 'judge' })"
      />
    </div>

    <div class="grid">
      <SectionCard title="快捷操作">
        <div class="actions">
          <button
            v-for="action in actions"
            :key="action.key"
            type="button"
            class="action"
            :class="ACTION_TONE[action.group]"
            :disabled="action.needsContest && !contest.currentId"
            @click="action.run()"
          >
            <span class="action-icon"><el-icon><component :is="action.icon" /></el-icon></span>
            <span class="action-label">{{ action.label }}</span>
          </button>
        </div>
      </SectionCard>

      <SectionCard title="召集机器">
        <template #extra>
          <el-button size="small" type="primary" plain @click="announceNow">
            立即召集
          </el-button>
        </template>
        <dl v-if="announce" class="kv">
          <dt>周期广播</dt>
          <dd>
            <el-tag size="small" :type="announce.enabled ? 'success' : 'info'">
              {{ announce.enabled ? '已开启' : '已关闭' }}
            </el-tag>
            <span class="muted"> UDP {{ announce.port }}</span>
          </dd>
          <dt>广播地址</dt>
          <dd class="mono">{{ announce.last_url || '—' }}</dd>
          <dt>上次广播</dt>
          <dd>{{ announce.last_sent_at ? since(announce.last_sent_at) : '尚未广播' }}</dd>
          <template v-if="announce.last_error">
            <dt>错误</dt>
            <dd class="error-text">{{ announce.last_error }}</dd>
          </template>
        </dl>
        <div v-else class="muted">—</div>
      </SectionCard>

      <SectionCard title="最近事件" class="span-2" flush>
        <template #extra>
          <el-button link type="primary" @click="router.push({ name: 'events' })">全部事件</el-button>
        </template>
        <ul v-if="events.length" class="events">
          <li v-for="item in events" :key="item.id">
            <span class="status-dot" :class="eventTone(item.level) === 'info' ? 'online' : 'warning'" />
            <el-tag size="small" :type="eventTone(item.level)" effect="plain">
              {{ eventCategoryLabel(item.category) }}
            </el-tag>
            <span v-if="item.player_no" class="mono">{{ item.player_no }}</span>
            <span class="event-msg">{{ item.message }}</span>
            <span class="event-time">{{ formatTime(item.ts) }}</span>
          </li>
        </ul>
        <div v-else class="empty-block">{{ contest.currentId ? '无事件' : '未选场次' }}</div>
      </SectionCard>
    </div>
  </PageShell>
</template>

<style scoped>
.contest-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.contest-name {
  color: var(--sj-text-2);
}
.clock {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 16px;
  border-radius: var(--sj-radius);
  background: var(--sj-surface);
  border: 1px solid var(--sj-border);
}
.clock-label {
  font-size: 13px;
  color: var(--sj-text-3);
}
.clock-value {
  font-family: var(--sj-mono);
  font-size: 22px;
  font-weight: 600;
}

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 16px;
}
.grid > :deep(.section-card + .section-card) {
  margin-top: 0;
}
.span-2 {
  grid-column: 1 / -1;
}
@media (max-width: 1100px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.actions {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.action {
  all: unset;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--sj-border);
  border-radius: var(--sj-radius-sm);
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.action:hover:not(:disabled) {
  border-color: var(--sj-primary);
  background: var(--sj-primary-soft);
}
.action:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.action:focus-visible {
  outline: 2px solid var(--sj-primary);
}
.action-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  font-size: 16px;
  flex-shrink: 0;
}
.primary .action-icon { background: var(--sj-primary-soft); color: var(--sj-primary); }
.success .action-icon { background: var(--sj-success-soft); color: var(--sj-success); }
.warning .action-icon { background: var(--sj-warning-soft); color: var(--sj-warning); }
.action-label {
  font-size: 13.5px;
  font-weight: 500;
}

.kv {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 10px 12px;
  margin: 0;
  font-size: 13px;
}
.kv dt {
  color: var(--sj-text-3);
}
.kv dd {
  margin: 0;
  word-break: break-all;
}

.events {
  list-style: none;
  margin: 0;
  padding: 0;
}
.events li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--sj-border);
  font-size: 13px;
}
.events li:last-child {
  border-bottom: none;
}
.event-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.event-time {
  color: var(--sj-text-3);
  font-size: 12px;
  white-space: nowrap;
}
</style>
