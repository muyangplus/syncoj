<script setup lang="ts">
/**
 * 一道题的「版本 × 分数」timeline（§12.9）：横轴是收到代码的时间，纵轴是该版本的分数。
 *
 * 内联 SVG，不引第三方图表库。形状与规矩：
 *
 * - 分数是"这一版代码在被下一版替换之前的成绩"，所以连线用**阶梯**（下一版到来时才跳）；
 * - 点的填充色表示评测状态（AC / WA / …，状态色只用在状态上），状态变化处直接标状态字，
 *   其余靠悬停提示与弹窗里的版本表 —— 颜色从不是唯一的信息来源；
 * - 没评过的版本画在基线下方的一条"未评"带上（空心小点），不冒充 0 分；
 * - 成绩矩阵当前采用的那一版加一圈外环；
 * - 悬停：竖向十字线吸附到最近的版本；点击选中（发 `select`）。
 *
 * 颜色全部取 `styles.css` 的 `--sj-*` 令牌，暗色模式随令牌切换。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type { SourceVersionOut } from '@/api/types'
import { formatTime } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    versions: SourceVersionOut[]
    selectedId?: number | null
    /** 小多图用：更矮、不画状态字与横轴刻度以外的装饰 */
    compact?: boolean
    height?: number
  }>(),
  { selectedId: null, compact: false, height: 0 },
)
const emit = defineEmits<{ (e: 'select', version: SourceVersionOut): void }>()

const root = ref<HTMLElement | null>(null)
const width = ref(600)
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!root.value) return
  width.value = root.value.clientWidth || 600
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver((entries) => {
      const next = Math.round(entries[0]?.contentRect.width ?? 0)
      if (next > 0) width.value = next
    })
    observer.observe(root.value)
  }
})
onBeforeUnmount(() => observer?.disconnect())

const H = computed(() => props.height || (props.compact ? 128 : 240))
const PAD = computed(() =>
  props.compact
    ? { left: 34, right: 12, top: 10, bottom: 34 }
    : { left: 44, right: 20, top: 24, bottom: 46 },
)
/** 未评带：基线下方的一条，放没评过的版本 */
const STRIP = computed(() => (props.compact ? 8 : 12))

interface Point {
  version: SourceVersionOut
  t: number
  x: number
  /** null = 没评过（画在未评带上） */
  y: number | null
  score: number | null
  status: string | null
  label: string | null
}

function judged(v: SourceVersionOut): boolean {
  return !!v.judge && v.judge.status === 'done' && v.judge.score !== null && v.judge.score !== undefined
}

const maxScore = computed(() => {
  let max = 0
  for (const v of props.versions) {
    if (v.judge?.max_score) max = Math.max(max, v.judge.max_score)
    if (v.judge?.score) max = Math.max(max, v.judge.score)
  }
  return max || 100
})

const plot = computed(() => {
  const pad = PAD.value
  return {
    x0: pad.left,
    x1: Math.max(pad.left + 10, width.value - pad.right),
    y0: pad.top,
    // 底部留出未评带与横轴刻度
    y1: H.value - pad.bottom,
  }
})

const domain = computed(() => {
  const times = props.versions.map((v) => new Date(v.received_at).getTime()).filter((t) => !Number.isNaN(t))
  if (!times.length) return { t0: 0, t1: 1 }
  let t0 = Math.min(...times)
  let t1 = Math.max(...times)
  if (t1 - t0 < 60_000) {
    // 一分钟之内的几版：两边各撑开半分钟，不挤成一根竖线
    t0 -= 30_000
    t1 += 30_000
  } else {
    const pad = (t1 - t0) * 0.04
    t0 -= pad
    t1 += pad
  }
  return { t0, t1 }
})

function xOf(t: number): number {
  const { x0, x1 } = plot.value
  const { t0, t1 } = domain.value
  return x0 + ((t - t0) / (t1 - t0)) * (x1 - x0)
}

function yOf(score: number): number {
  const { y0, y1 } = plot.value
  return y1 - (Math.max(0, Math.min(score, maxScore.value)) / maxScore.value) * (y1 - y0)
}

const points = computed<Point[]>(() => {
  let previous: string | null = null
  return props.versions.map((version) => {
    const t = new Date(version.received_at).getTime()
    const ok = judged(version)
    const status = version.judge?.outcome ?? null
    const score = ok ? (version.judge!.score as number) : null
    // 状态字只标在"变了"的地方：每个点都标就没人读了
    const label = ok && status && status !== previous ? status : null
    if (ok) previous = status
    return { version, t, x: xOf(t), y: ok ? yOf(score as number) : null, score, status, label }
  })
})

/** 阶梯线：每个已评版本的分数一直持续到下一个已评版本 */
const stepPath = computed(() => {
  const judgedPoints = points.value.filter((p) => p.y !== null)
  if (!judgedPoints.length) return ''
  let d = `M${judgedPoints[0].x},${judgedPoints[0].y}`
  for (let i = 1; i < judgedPoints.length; i++) {
    d += ` H${judgedPoints[i].x} V${judgedPoints[i].y}`
  }
  return d
})

const yTicks = computed(() => {
  const max = maxScore.value
  const ticks = props.compact ? [0, max] : [0, Math.round(max / 2), max]
  return Array.from(new Set(ticks)).map((value) => ({ value, y: yOf(value) }))
})

function clock(t: number, withDate: boolean): string {
  const text = formatTime(new Date(t).toISOString())
  // formatTime 给 "YYYY-MM-DD HH:mm:ss"
  return withDate ? text.slice(5, 16) : text.slice(11, 16)
}

const xTicks = computed(() => {
  const { t0, t1 } = domain.value
  const { x0, x1 } = plot.value
  const count = Math.max(2, Math.min(props.compact ? 3 : 6, Math.floor((x1 - x0) / 90)))
  const sameDay = clock(t0, true).slice(0, 5) === clock(t1, true).slice(0, 5)
  const ticks = []
  for (let i = 0; i < count; i++) {
    const t = t0 + ((t1 - t0) * (i + 0.5)) / count
    ticks.push({ x: xOf(t), text: clock(t, !sameDay) })
  }
  return ticks
})

function statusClass(status: string | null): string {
  switch (status) {
    case 'AC':
      return 'is-ac'
    case 'WA':
    case 'RE':
      return 'is-bad'
    case 'TLE':
    case 'MLE':
    case 'OLE':
      return 'is-limit'
    default:
      return 'is-other'
  }
}

// ---- 悬停 ----

const hover = ref<Point | null>(null)

function onMove(event: PointerEvent): void {
  const svg = event.currentTarget as SVGSVGElement
  const box = svg.getBoundingClientRect()
  const x = ((event.clientX - box.left) / box.width) * width.value
  let best: Point | null = null
  for (const point of points.value) {
    if (!best || Math.abs(point.x - x) < Math.abs(best.x - x)) best = point
  }
  hover.value = best
}

function onLeave(): void {
  hover.value = null
}

function onClick(): void {
  if (hover.value) emit('select', hover.value.version)
}

function onKey(event: KeyboardEvent): void {
  const list = points.value
  if (!list.length) return
  const index = hover.value ? list.indexOf(hover.value) : -1
  if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
    event.preventDefault()
    const step = event.key === 'ArrowRight' ? 1 : -1
    hover.value = list[Math.max(0, Math.min(list.length - 1, index < 0 ? 0 : index + step))]
  } else if ((event.key === 'Enter' || event.key === ' ') && hover.value) {
    event.preventDefault()
    emit('select', hover.value.version)
  }
}

const tooltipStyle = computed(() => {
  const point = hover.value
  if (!point) return {}
  const left = point.x > width.value * 0.6 ? undefined : `${point.x + 12}px`
  const right = point.x > width.value * 0.6 ? `${width.value - point.x + 12}px` : undefined
  return { left, right, top: `${PAD.value.top}px` }
})

const TRIGGER: Record<string, string> = { auto: '自动', manual: '手动', rejudge: '历史版本重测' }

const ariaLabel = computed(() => {
  const judgedCount = points.value.filter((p) => p.y !== null).length
  return `${props.versions.length} 个版本，${judgedCount} 个已评测`
})
</script>

<template>
  <div ref="root" class="timeline" :class="{ compact }">
    <svg
      :width="width"
      :height="H"
      :viewBox="`0 0 ${width} ${H}`"
      role="img"
      :aria-label="ariaLabel"
      tabindex="0"
      @pointermove="onMove"
      @pointerleave="onLeave"
      @click="onClick"
      @keydown="onKey"
      @blur="onLeave"
    >
      <!-- 网格与纵轴刻度 -->
      <g class="grid">
        <line
          v-for="tick in yTicks"
          :key="`g${tick.value}`"
          :x1="plot.x0"
          :x2="plot.x1"
          :y1="tick.y"
          :y2="tick.y"
        />
      </g>
      <g class="axis-text">
        <text v-for="tick in yTicks" :key="`y${tick.value}`" :x="plot.x0 - 6" :y="tick.y + 4" text-anchor="end">
          {{ tick.value }}
        </text>
        <text v-for="(tick, i) in xTicks" :key="`x${i}`" :x="tick.x" :y="H - 6" text-anchor="middle">
          {{ tick.text }}
        </text>
      </g>

      <!-- 未评带 -->
      <line class="strip" :x1="plot.x0" :x2="plot.x1" :y1="plot.y1 + STRIP" :y2="plot.y1 + STRIP" />

      <!-- 十字线 -->
      <line
        v-if="hover"
        class="crosshair"
        :x1="hover.x"
        :x2="hover.x"
        :y1="plot.y0"
        :y2="plot.y1 + STRIP + 4"
      />

      <path v-if="stepPath" class="line" :d="stepPath" />

      <g v-for="point in points" :key="point.version.id">
        <circle
          v-if="point.y === null"
          class="pending"
          :cx="point.x"
          :cy="plot.y1 + STRIP"
          :r="compact ? 3 : 3.5"
        />
        <template v-else>
          <circle
            v-if="point.version.in_matrix"
            class="matrix-ring"
            :cx="point.x"
            :cy="point.y"
            :r="compact ? 7 : 8.5"
          />
          <circle
            class="dot"
            :class="[statusClass(point.status), { selected: point.version.id === selectedId }]"
            :cx="point.x"
            :cy="point.y"
            :r="compact ? 3.5 : 4.5"
          />
          <text
            v-if="!compact && point.label"
            class="status-label"
            :x="point.x"
            :y="point.y - 10"
            text-anchor="middle"
          >
            {{ point.label }}
          </text>
        </template>
      </g>
    </svg>

    <div v-if="hover" class="tooltip" :style="tooltipStyle">
      <div class="tip-value">
        <template v-if="hover.score !== null">{{ hover.score }} / {{ hover.version.judge?.max_score ?? '?' }}</template>
        <template v-else-if="hover.version.judge">{{ hover.version.judge.status === 'failed' ? '评测失败' : '评测中' }}</template>
        <template v-else>未评测</template>
        <span v-if="hover.status" class="tip-status">{{ hover.status }}</span>
      </div>
      <div class="tip-row">{{ formatTime(hover.version.received_at) }}</div>
      <div class="tip-row mono">{{ hover.version.sha256.slice(0, 8) }} · {{ hover.version.rel_path }}</div>
      <div v-if="hover.version.judge" class="tip-row">
        {{ TRIGGER[hover.version.judge.trigger] ?? hover.version.judge.trigger
        }}<template v-if="hover.version.judge.reused"> · 复用</template
        ><template v-if="hover.version.in_matrix"> · 计入成绩</template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  position: relative;
  width: 100%;
}
svg {
  display: block;
  outline: none;
  cursor: pointer;
}
svg:focus-visible {
  outline: 2px solid var(--sj-primary);
  outline-offset: 2px;
  border-radius: var(--sj-radius-sm);
}
.grid line {
  stroke: var(--sj-border);
  stroke-width: 1;
}
.axis-text text {
  fill: var(--sj-text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.strip {
  stroke: var(--sj-border);
  stroke-width: 1;
}
.crosshair {
  stroke: var(--sj-text-3);
  stroke-width: 1;
}
.line {
  fill: none;
  stroke: var(--sj-primary);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.dot {
  stroke: var(--sj-surface);
  stroke-width: 2;
}
.dot.is-ac {
  fill: var(--sj-success);
}
.dot.is-bad {
  fill: var(--sj-danger);
}
.dot.is-limit {
  fill: var(--sj-warning);
}
.dot.is-other {
  fill: var(--sj-text-3);
}
.dot.selected {
  stroke: var(--sj-text);
}
.matrix-ring {
  fill: none;
  stroke: var(--sj-text);
  stroke-width: 1.5;
}
.pending {
  fill: var(--sj-surface);
  stroke: var(--sj-text-3);
  stroke-width: 1.5;
}
.status-label {
  fill: var(--sj-text-2);
  font-size: 11px;
  font-weight: 600;
}
.tooltip {
  position: absolute;
  z-index: 2;
  min-width: 160px;
  max-width: 320px;
  padding: 8px 10px;
  border-radius: var(--sj-radius-sm);
  border: 1px solid var(--sj-border);
  background: var(--sj-surface);
  box-shadow: var(--sj-shadow-lg);
  pointer-events: none;
  font-size: 12px;
  color: var(--sj-text-2);
}
.tip-value {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--sj-text);
}
.tip-status {
  font-size: 12px;
  color: var(--sj-text-2);
}
.tip-row {
  margin-top: 2px;
  word-break: break-all;
}
.mono {
  font-family: var(--sj-mono);
}
</style>
