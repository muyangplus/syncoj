import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
import '@/styles.css'

import App from '@/App.vue'
import { router } from '@/router'
import { metaApi } from '@/api'
import { setUnauthorizedHandler } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { applyMeta } from '@/utils/format'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

// 401 的统一处理：清掉本地状态并回登录页。
// 注册在 client 而不是直接 import router —— 那会形成循环依赖。
setUnauthorizedHandler(() => {
  useAuthStore().clear()
  const current = router.currentRoute.value
  if (current.name !== 'login') {
    void router.replace({ name: 'login', query: { redirect: current.fullPath } })
  }
})

// 显示时区由服务端下发（`GET /api/v1/meta`）。**取不到就按默认的 +08:00 继续**：
// 一个为了对时区的额外请求不该把整页卡在启动阶段，而默认值正好是考区那只钟。
void metaApi
  .get()
  .then((meta) => applyMeta(meta.display_utc_offset_minutes, meta.display_timezone))
  .catch(() => {
    /* 保持默认值；页面上时间仍然是考区的钟点 */
  })

app.mount('#app')
