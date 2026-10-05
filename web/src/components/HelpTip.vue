<script setup lang="ts">
/**
 * 标题或字段旁边的小问号。
 *
 * 成段的背景说明挂在这上面，而不是铺在正文里。理由是教师扫一页时的顺序：
 * 先找按钮，找不到才去找说明。正文里堆着三行"为什么这样设计"，按钮就被
 * 推到视线之外了；而把说明收进问号，需要的人点一下就有，不需要的人不被挡。
 *
 * 默认插槽可以带 `<strong>`、`<br>` 这类富文本 —— 有些说明（比如"目录要指向
 * 哪里"）用等宽片段才说得清楚。`content` 属性是给纯一句话的省事写法。
 */
defineProps<{
  /** 纯文本说明。要与 `content` 二选一；都不给就是个空问号。 */
  content?: string
  /** 气泡最大宽度。默认 360px，够放三行中文。 */
  width?: number
}>()
</script>

<template>
  <el-tooltip placement="top" :show-after="120">
    <template #content>
      <div class="help-tip-body" :style="{ maxWidth: `${width ?? 360}px` }">
        <slot>{{ content }}</slot>
      </div>
    </template>
    <el-icon class="help-tip-icon"><QuestionFilled /></el-icon>
  </el-tooltip>
</template>

<style scoped>
.help-tip-icon {
  color: #909399;
  cursor: help;
  font-size: 14px;
  vertical-align: middle;
}

.help-tip-body {
  line-height: 1.6;
  white-space: normal;
}
</style>
