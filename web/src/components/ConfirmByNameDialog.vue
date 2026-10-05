<script setup lang="ts">
import { computed, ref, watch } from 'vue'

/**
 * 硬删除前的"把名字打一遍"确认。
 *
 * 为什么不用 `ElMessageBox.confirm`：那个弹窗的确认按钮在连续操作里会被手指
 * 肌肉记忆点掉（同一个位置、同一个形状、点三次之后就不再看字了）。而"打一遍
 * 名字"要求眼睛回到屏幕上、手做一件不可自动化的事 —— 它挡住的正是"删错对象"
 * 这类不可逆事故。
 *
 * 这一份确认是**给教师看的**。真正的把关在服务端：`confirm` 会随请求发过去，
 * 逐字比对，对不上就 400。界面上的输入框只是让他在提交前就发现打错了，
 * 而不是让界面成为唯一的一道锁 —— 绕过界面的调用也必须一样安全。
 */
const props = defineProps<{
  modelValue: boolean
  title: string
  /** 必须逐字打出来的标识（选手用考号、场次用标识、名单用名称、机器用主机名）。 */
  expected: string
  /** 一句话说清后果，尤其是"什么不会被删"。 */
  detail?: string
  /**
   * 影响面：逐条写清这次会动到多少东西。
   *
   * 数得出来的必须数出来（「清空这 37 名选手的代码文件」「其中 5 人已有成绩，
   * 会保留」）。让人自己推算影响面，等于让他赌一把 —— 而这一个弹窗之后
   * 就没有回头路了。数不出来的时候也照实说（「条数要看服务端返回」），
   * 不要含糊成"可能会影响一些数据"。
   */
  impact?: string[]
  confirmText?: string
  submitting?: boolean
  /** 输入的提示文案，默认提示打哪个值 */
  placeholder?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  confirm: []
}>()

const typed = ref('')

// 每次打开都从空白开始：留着上次的内容会让"再确认一次"变成直接点确认
watch(
  () => props.modelValue,
  (open) => {
    if (open) typed.value = ''
  },
)

const matches = computed(() => typed.value === props.expected)

function close(): void {
  emit('update:modelValue', false)
}

function submit(): void {
  if (!matches.value) return
  emit('confirm')
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="480px"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
  >
    <el-alert type="warning" :closable="false" show-icon>
      <template #title>这个操作不可撤销</template>
      <template #default>
        <div v-if="detail">{{ detail }}</div>
        <ul v-if="impact?.length" class="impact">
          <li v-for="line in impact" :key="line">{{ line }}</li>
        </ul>
        <div style="margin-top: 6px">
          请把
          <strong class="mono">{{ expected }}</strong>
          原样打一遍以确认。
        </div>
      </template>
    </el-alert>

    <el-input
      v-model="typed"
      class="mono-input"
      :placeholder="placeholder ?? `输入 ${expected}`"
      clearable
      style="margin-top: 14px"
      @keyup.enter="submit"
    />

    <!-- 少数清空入口需要额外的选项（比如"连未消失的一起清"） -->
    <div v-if="$slots.default" style="margin-top: 12px">
      <slot />
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="danger" :disabled="!matches" :loading="submitting" @click="submit">
        {{ confirmText ?? '确认删除' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

/* 影响面单独成块：它和上面那句"不可撤销"是两件事，揉在一起会被一起略过 */
.impact {
  margin: 6px 0 0;
  padding-left: 18px;
}

.impact li {
  margin: 2px 0;
}
</style>
