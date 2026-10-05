<script setup lang="ts" generic="T">
/**
 * 带分页的表格。
 *
 * 它包住的是每个列表页都会重复的那十几行：`el-table` 的样式参数、选择列、
 * 空状态、分页器。列本身仍然由页面用 `<el-table-column>` 写在自己的
 * `<template #columns>` 里 —— 列是页面最私有的部分，抽象它不是省事而是找麻烦。
 *
 * **选择列的两个属性不是可选项**：
 *
 * * `row-key` —— 没有它，el-table 只能按对象引用认行，而每一轮刷新回来的
 *   都是新对象，勾选会当场消失
 * * `reserve-selection` —— 没有它，翻页或数据被替换时勾选一样会丢
 *
 * 这两条合起来才让"勾 10 个人 → 翻页核对 → 回来批量删"这条真实流程可行。
 */
const props = defineProps<{
  rows: T[]
  rowKey: (row: T) => string | number
  loading?: boolean
  selectable?: boolean
  /** 总条数。分页器用它算页数，**不是** `rows.length`。 */
  total?: number
  page?: number
  pageSize?: number
  /**
   * 数据已经就绪但一条都没有时的说明。
   *
   * 写**下一步做什么**，不要写"暂无数据"：空列表几乎总是"还没做上一件事"
   * （还没签发密钥、还没导名单、还没应用名单），一句"暂无数据"只是把
   * "接下来该点哪里"留给教师自己推。
   */
  emptyText?: string
  /** 空状态的行动按钮文案。给了就显示按钮并 emit `empty-action`。 */
  emptyActionText?: string
  /**
   * 筛选之后为空、但**不过滤**其实有数据时用这句。
   *
   * 两者必须分开：把"筛掉了"说成"没有数据"，教师会去查系统为什么不收代码，
   * 而真正的原因只是筛选框里还留着一个上一位教师输的条件。
   */
  emptyFilteredText?: string
  /** 当前是否处于筛选状态（决定空状态用哪句话）。 */
  filtered?: boolean
}>()

const emit = defineEmits<{
  'update:page': [number]
  'update:pageSize': [number]
  'selection-change': [T[]]
  'empty-action': []
}>()

const paginated = () => props.total !== undefined && props.pageSize !== undefined
</script>

<template>
  <div>
    <el-table
      :data="rows"
      :row-key="rowKey"
      v-loading="loading"
      border
      stripe
      size="small"
      @selection-change="(picked: T[]) => emit('selection-change', picked)"
    >
      <el-table-column v-if="selectable" type="selection" reserve-selection width="44" />
      <slot name="columns" />

      <template #empty>
        <div class="empty-block">
          <slot name="empty">
            <p class="empty-text">
              {{ (filtered && emptyFilteredText) || emptyText || '没有数据' }}
            </p>
            <el-button
              v-if="emptyActionText && !(filtered && emptyFilteredText)"
              type="primary"
              plain
              size="small"
              @click="emit('empty-action')"
            >
              {{ emptyActionText }}
            </el-button>
          </slot>
        </div>
      </template>
    </el-table>

    <div v-if="paginated()" class="pager">
      <el-pagination
        size="small"
        background
        layout="total, sizes, prev, pager, next, jumper"
        :total="total ?? 0"
        :current-page="page ?? 1"
        :page-size="pageSize ?? 50"
        :page-sizes="[20, 50, 100, 200, 500]"
        @current-change="(value: number) => emit('update:page', value)"
        @size-change="(value: number) => emit('update:pageSize', value)"
      />
    </div>
  </div>
</template>

<style scoped>
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.empty-text {
  margin: 0 0 12px;
}
</style>
