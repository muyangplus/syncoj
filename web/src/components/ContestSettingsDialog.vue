<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import { contestApi, rosterApi } from '@/api'
import type { ApplyRosterOut, ContestOut, ContestUpdate, RosterOut } from '@/api/types'
import FormDialog from '@/components/FormDialog.vue'
import { useMutation } from '@/composables/useMutation'

const props = defineProps<{
  modelValue: boolean
  contest: ContestOut | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [contest: ContestOut]
  /** 选手被这次操作改动了，列表该重新拉一遍。 */
  changed: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const rosters = ref<RosterOut[]>([])
/** 最近一次「按这份名单补人」的回执。留在弹窗里，教师能看到到底动了多少人。 */
const applyReceipt = ref('')

const STATUSES: Array<{ value: string; label: string }> = [
  { value: 'running', label: '进行中' },
  { value: 'draft', label: '筹备中' },
  { value: 'frozen', label: '已封榜 · 停止下发' },
  { value: 'closed', label: '已结束 · 停止扫描成绩' },
  { value: 'archived', label: '已归档 · 不再解析、扫描、评测' },
]

const form = reactive({
  name: '',
  status: 'draft',
  note: '',
  /**
   * 考试时间窗。存的是 `Date | null`：`null` = 这一端不限制。
   * 从服务端回来的是带 Z 的 UTC 字符串，`new Date(...)` 之后 el-date-picker
   * 显示的就是本机时区的那个时刻 —— 教师看到的就是他刚才填的那个点。
   */
  startsAt: null as Date | null,
  endsAt: null as Date | null,
  defaultRosterId: null as number | null,
  /**
   * 考生页密码。**只写不读**：服务端只回 `has_player_password`。
   * 留空 = 不动；填了 = 替换；`clearPlayerPassword` = 清除（关掉考生登录）。
   */
  playerPassword: '',
  clearPlayerPassword: false,
})

/** 填了新密码但不足 4 位：服务端会 422，在本地就拦住保存按钮。 */
const playerPasswordTooShort = computed(
  () => form.playerPassword.length > 0 && form.playerPassword.length < 4,
)

watch(visible, async (open) => {
  if (!open || !props.contest) return
  form.name = props.contest.name
  form.status = props.contest.status
  form.note = props.contest.note ?? ''
  form.startsAt = props.contest.starts_at ? new Date(props.contest.starts_at) : null
  form.endsAt = props.contest.ends_at ? new Date(props.contest.ends_at) : null
  form.defaultRosterId = props.contest.default_roster_id ?? null
  form.playerPassword = ''
  form.clearPlayerPassword = false
  applyReceipt.value = ''
  try {
    const page = await rosterApi.list({ limit: 500 })
    rosters.value = page.items
  } catch {
    // 拉不到名单不拦着改状态：名称、状态都不依赖它
    rosters.value = []
  }
})

const saveContest = useMutation(
  (payload: ContestUpdate) => {
    const contest = props.contest
    if (!contest) throw new Error('没有选中场次')
    return contestApi.update(contest.id, payload)
  },
  {
    success: '已保存',
    onDone: (saved) => {
      visible.value = false
      emit('saved', saved)
    },
  },
)

/**
 * 按选中的名单把选手补进场次。**只补不删。**
 *
 * 这个按钮存在的理由：选了「默认名单」之后选手不会自己出现，而此前唯一的
 * 应用入口在「名单库」页 —— 教师的自然路径是场次设置 → 选名单 → 保存 →
 * 去选手状态 → 空的，中间没有任何东西告诉他少了一步。
 *
 * `prune` 固定 false：要清理必须去名单库走那个显式确认过的「应用并清理」。
 * 一个叫「补人」的按钮顺手删掉几个人的提交是不可接受的。
 */
const applyRoster = useMutation(
  (rosterId: number) => {
    const contest = props.contest
    if (!contest) throw new Error('没有选中场次')
    return rosterApi.applyToContest(contest.id, { roster_id: rosterId, prune: false })
  },
  {
    success: (report) => {
      applyReceipt.value = describeApply(report)
      return applyReceipt.value
    },
    onDone: () => emit('changed'),
  },
)

function describeApply(report: ApplyRosterOut): string {
  const bits: string[] = []
  if (report.created) bits.push(`新建 ${report.created} 人`)
  if (report.updated) bits.push(`更新 ${report.updated} 人`)
  if (report.kept) bits.push(`保留 ${report.kept} 人`)
  const protectedCount = report.protected?.length ?? 0
  if (protectedCount) bits.push(`${protectedCount} 人有提交，未动`)
  return bits.length ? `已补人：${bits.join('、')}` : '无改动'
}

async function submit(): Promise<void> {
  await saveContest.run({
    name: form.name.trim(),
    status: form.status,
    note: form.note.trim(),
    // **显式传 null**（不是省掉这个字段）：服务端按 `model_fields_set` 判断，
    // "传了 null" = 清掉这一端的时间限制，"没传" = 这次别动它。清空日期选择框
    // 就该是前者 —— 否则教师永远删不掉一个填错的时间点。
    starts_at: form.startsAt ? form.startsAt.toISOString() : null,
    ends_at: form.endsAt ? form.endsAt.toISOString() : null,
    // null 分不清"没传"和"要清空"，所以清空走显式开关
    clear_default_roster: form.defaultRosterId === null,
    default_roster_id: form.defaultRosterId,
    // 只写不读：没填就不传（不动），清除走显式开关
    clear_player_password: form.clearPlayerPassword,
    ...(form.playerPassword && !form.clearPlayerPassword
      ? { player_password: form.playerPassword }
      : {}),
  })
}
</script>

<template>
  <FormDialog
    v-model="visible"
    title="场次设置"
    width="560px"
    :submitting="saveContest.pending.value"
    :disabled="!form.name.trim() || playerPasswordTooShort"
    @submit="submit"
  >
    <el-form label-width="90px">
      <el-form-item label="名称">
        <el-input v-model="form.name" maxlength="200" />
      </el-form-item>

      <el-form-item label="标识">
        <el-input :model-value="contest?.slug ?? ''" disabled />
      </el-form-item>

      <el-form-item label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option
            v-for="item in STATUSES"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="开考时间">
        <el-date-picker
          v-model="form.startsAt"
          type="datetime"
          placeholder="不限"
          clearable
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="结束时间">
        <el-date-picker
          v-model="form.endsAt"
          type="datetime"
          placeholder="不限"
          clearable
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="默认名单">
        <el-select
          v-model="form.defaultRosterId"
          placeholder="不指定"
          clearable
          style="width: 100%"
        >
          <el-option
            v-for="roster in rosters"
            :key="roster.id"
            :label="`${roster.name}（${roster.entry_count} 人）`"
            :value="roster.id"
          />
        </el-select>
        <!-- 选名单不改选手，补人要显式点 -->
        <div v-if="form.defaultRosterId !== null" class="apply-row">
          <el-button
            :loading="applyRoster.pending.value"
            @click="applyRoster.run(form.defaultRosterId as number)"
          >
            按名单补人
          </el-button>
          <span class="cell-sub">只补不删</span>
        </div>
        <div v-if="applyReceipt" class="cell-sub apply-receipt">{{ applyReceipt }}</div>
        <div v-if="contest?.player_count" class="cell-sub">
          当前选手 {{ contest.player_count }} 名
        </div>
      </el-form-item>

      <el-form-item label="考生页密码">
        <el-input
          v-model="form.playerPassword"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="128"
          :disabled="form.clearPlayerPassword"
          :placeholder="contest?.has_player_password ? '已设置 · 留空不改' : '未设置'"
        />
        <div v-if="contest?.has_player_password" class="apply-row">
          <el-checkbox v-model="form.clearPlayerPassword">清除密码</el-checkbox>
          <span v-if="form.clearPlayerPassword" class="cell-sub">考生将无法用准考证号登录考生页</span>
        </div>
        <div v-if="playerPasswordTooShort" class="cell-sub password-warn">至少 4 位</div>
      </el-form-item>

      <el-form-item label="备注">
        <el-input
          v-model="form.note"
          type="textarea"
          :rows="2"
          maxlength="500"
        />
      </el-form-item>
    </el-form>
  </FormDialog>
</template>

<style scoped>
.apply-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.password-warn {
  color: var(--el-color-danger);
}

.apply-receipt {
  color: var(--el-color-success);
}
</style>
