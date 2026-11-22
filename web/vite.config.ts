import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

import { buildInfo } from './scripts/build-info-plugin.mjs'
import { thirdPartyLicenses } from './scripts/license-plugin.mjs'

const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'))

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(version),
  },
  // thirdPartyLicenses：把打进产物的第三方包许可证全文写到 dist/assets/third-party-licenses.txt
  // buildInfo：写 dist/build-info.json（版本 + 源码指纹），服务端启动时据此判断前端是否需要重新构建
  plugins: [vue(), thirdPartyLicenses(), buildInfo({ root: fileURLToPath(new URL('.', import.meta.url)) })],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    watch: {
      ignored: [
        '**/node_modules/**',
        '**/.git/**',
        '**/*.tmp',
        '**/*.tmpdir/**',
        '**/.*.tmpdir/**',
      ],
    },
    // 开发时把 API 请求代理到后端。
    // 不用 CORS：生产环境前端由 FastAPI 同源托管，开发时也保持同源，
    // 这样"开发能跑、上线跨域报错"这类问题不会出现。
    proxy: {
      '/api': {
        target: process.env.SYNCOJ_API || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/healthz': {
        target: process.env.SYNCOJ_API || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 产物由服务端 FastAPI 在根路径托管，资源用相对根路径
    assetsDir: 'assets',
    // Element Plus 体积不小，默认 500KB 的告警没意义
    chunkSizeWarningLimit: 2000,
  },
})
