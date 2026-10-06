<script setup lang="ts">
/**
 * 页面外壳：标题 + 一句说明 + 工具条 + 错误条。
 *
 * `hint` 只放**一句话**（这一页管什么、去哪儿点）。**曾经还有一个 `#hint` 插槽
 * 用来在标题旁边挂一个问号放长文说明 —— 已经删掉**：长文说明在界面上就没人看，
 * 而"没人看的说明"和"没有说明"在使用上是同一件事，却会让页面看起来更可信地
 * 答非所问。背景说明属于文档（DESIGN/README），不属于界面。
 *
 * 错误条自带"重试"。没有它时，列表加载失败后教师唯一能做的事是刷新整个
 * 浏览器，而一个按钮比一句"加载失败"有用得多。错误文案由页面给（服务端的
 * `detail` 就是人话），这里只负责把它和下一步动作摆在一起。
 */
defineProps<{
  title: string
  /** 一句话。超一行就不该放这里，该去改文档或删掉。 */
  hint?: string
  /** 列表加载失败时的那句话。有值就显示。 */
  error?: string | null
  /** 错误条上那句"接下来怎么办"。 */
  errorAction?: string
  /** 给了就在错误条上显示「重试」按钮。 */
  retryable?: boolean
}>()

const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div>
    <div class="page-header">
      <div class="page-heading">
        <h2 class="page-title">{{ title }}</h2>
        <p v-if="hint" class="page-hint">{{ hint }}</p>
        <!-- 标题下的一行状态/补充。正文级的东西不要塞进标题，那是排版事故 -->
        <slot name="sub" />
      </div>
      <div class="toolbar">
        <slot name="toolbar" />
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      style="margin-bottom: 12px"
    >
      <template v-if="errorAction || retryable" #default>
        <div class="error-actions">
          <span v-if="errorAction" class="error-hint">{{ errorAction }}</span>
          <el-button v-if="retryable" size="small" type="primary" plain @click="emit('retry')">
            重试
          </el-button>
        </div>
      </template>
    </el-alert>

    <slot />
  </div>
</template>

<style scoped>
.page-heading {
  min-width: 0;
}

.page-title {
  display: flex;
  align-items: center;
  gap: 6px;
}

.error-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.error-hint {
  opacity: 0.9;
}
</style>
