<script setup lang="ts">
/**
 * 表单弹窗的外壳：标题 + 内容 + 取消/提交。
 *
 * 只统一"弹窗骨架 + 按钮的 loading/禁点"。字段与校验规则仍然由页面用
 * `el-form` 自己写 —— 把表单字段做成"传一个 schema 进来"看起来很省事，
 * 但一旦某页需要"输入 A 时才显示 B"或自定义校验，那套 schema 就撑不住了，
 * 最后还是要在页面里写特例。
 *
 * 提交时**只发一次**：`submitting` 期间按钮置灰并转圈，
 * 否则连续回车会建出两份一模一样的数据。
 */
defineProps<{
  modelValue: boolean
  title: string
  width?: string
  submitting?: boolean
  confirmText?: string
  /** 提交按钮是否可点。表单没填完时用一个计算值传进来。 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  submit: []
}>()
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    :width="width ?? '520px'"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
  >
    <slot />

    <template #footer>
      <slot name="footer-prepend" />
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="disabled"
        @click="emit('submit')"
      >
        {{ confirmText ?? '保存' }}
      </el-button>
    </template>
  </el-dialog>
</template>
