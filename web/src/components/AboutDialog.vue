<script setup lang="ts">
/** 关于与第三方许可：版本、项目许可证、依赖清单与许可证全文入口。 */
import { computed } from 'vue'

import { RUNTIME_CREDITS } from '@/legal/credits.generated'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const version = __APP_VERSION__
const REPO = 'https://github.com/muyangplus/syncoj'
</script>

<template>
  <el-dialog v-model="visible" title="关于与许可" width="640px">
    <div class="head">
      <span class="mark">S</span>
      <div>
        <div class="name">SyncOJ <span class="muted">{{ version }}</span></div>
        <div class="muted">
          许可证 Apache-2.0 ·
          <a :href="REPO" target="_blank" rel="noopener">源码</a>
        </div>
      </div>
    </div>

    <div class="section-label">第三方组件</div>
    <el-table :data="RUNTIME_CREDITS" max-height="320">
      <el-table-column prop="component" label="组件" width="150" />
      <el-table-column prop="name" label="依赖" min-width="170" />
      <el-table-column prop="version" label="版本" width="120" />
      <el-table-column prop="license" label="许可证" min-width="150" />
    </el-table>

    <div class="links">
      <a href="/assets/third-party-licenses.txt" target="_blank" rel="noopener">前端产物许可证全文</a>
      <a :href="`${REPO}/blob/main/THIRD-PARTY-LICENSES.md`" target="_blank" rel="noopener">THIRD-PARTY-LICENSES.md</a>
      <a :href="`${REPO}/blob/main/LICENSE`" target="_blank" rel="noopener">LICENSE</a>
    </div>

    <template #footer>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: var(--sj-brand-gradient);
  color: #fff;
  font-weight: 700;
  font-size: 18px;
}
.name {
  font-size: 16px;
  font-weight: 650;
}
.muted {
  color: var(--sj-text-3);
  font-size: 13px;
  font-weight: normal;
}
.section-label {
  margin: 16px 0 8px;
  font-weight: 600;
}
.links {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 12px;
  font-size: 13px;
}
a {
  color: var(--sj-primary);
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
</style>
