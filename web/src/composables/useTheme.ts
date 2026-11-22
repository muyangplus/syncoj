import { ref, watch } from 'vue'

const KEY = 'syncoj.theme'
type Theme = 'light' | 'dark'

function initial(): Theme {
  try {
    const saved = window.localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    /* 隐私模式 */
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

const theme = ref<Theme>(initial())

function apply(value: Theme): void {
  document.documentElement.classList.toggle('dark', value === 'dark')
}

apply(theme.value)
watch(theme, (value) => {
  apply(value)
  try {
    window.localStorage.setItem(KEY, value)
  } catch {
    /* 隐私模式 */
  }
})

/** 明暗主题：跟随系统，手动切换后记住选择。 */
export function useTheme() {
  return {
    theme,
    toggle: () => {
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
    },
  }
}
