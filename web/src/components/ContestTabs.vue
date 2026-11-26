<script setup lang="ts">
/** 场次页的二级菜单：场次 / 已归档。已归档不在侧栏，只从这里进。 */
import { useRoute, useRouter } from 'vue-router'

defineProps<{ archivedCount?: number | null }>()

const route = useRoute()
const router = useRouter()

function go(name: string | number | boolean | undefined) {
  if (typeof name === 'string' && name !== route.name) router.push({ name })
}
</script>

<template>
  <el-radio-group :model-value="route.name" size="small" class="contest-tabs" @update:model-value="go">
    <el-radio-button value="contests">场次</el-radio-button>
    <el-radio-button value="contests-archived">
      {{ archivedCount ? `已归档 · ${archivedCount}` : '已归档' }}
    </el-radio-button>
  </el-radio-group>
</template>

<style scoped>
.contest-tabs {
  margin-top: 8px;
}
</style>
