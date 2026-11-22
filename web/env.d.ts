/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

/** 构建时由 vite.config.ts 从 package.json 注入的版本号 */
declare const __APP_VERSION__: string
