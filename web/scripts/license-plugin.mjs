/**
 * 构建时汇总打进产物的第三方包许可证全文，输出 dist/assets/third-party-licenses.txt。
 *
 * 依据：产物里每个模块的路径。凡位于 node_modules 下的模块，取其所属包的
 * package.json（名称、版本、license 字段）与包目录里的许可证文件全文。
 * 找不到许可证全文的包让构建失败，除非在 FALLBACK_TEXTS 里给出原文。
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const LICENSE_FILE = /^(licen[cs]e|copying|notice)(\.(md|txt|markdown))?$/i

/** 包里没带许可证文件时使用的原文（必须是该包官方仓库的许可证原文）。 */
const FALLBACK_TEXTS = {}

function packageRoot(moduleId) {
  const id = moduleId.replace(/\\/g, '/').replace(/^\0/, '').split('?')[0]
  const marker = '/node_modules/'
  const at = id.lastIndexOf(marker)
  if (at < 0) return null
  const rest = id.slice(at + marker.length).split('/')
  const depth = rest[0].startsWith('@') ? 2 : 1
  if (rest.length <= depth) return null
  return id.slice(0, at + marker.length) + rest.slice(0, depth).join('/')
}

function licenseFiles(dir) {
  return readdirSync(dir)
    .filter((name) => LICENSE_FILE.test(name))
    .sort()
    .map((name) => ({ name, text: readFileSync(join(dir, name), 'utf8').trim() }))
}

function describe(dir) {
  const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'))
  let license = pkg.license
  if (license && typeof license === 'object') license = license.type
  if (!license && Array.isArray(pkg.licenses)) license = pkg.licenses.map((l) => l.type).join(' OR ')
  const repo = typeof pkg.repository === 'string' ? pkg.repository : pkg.repository?.url
  return { name: pkg.name, version: pkg.version, license: license || 'UNKNOWN', homepage: pkg.homepage || repo || '' }
}

export function thirdPartyLicenses({ fileName = 'assets/third-party-licenses.txt' } = {}) {
  return {
    name: 'syncoj-third-party-licenses',
    apply: 'build',
    generateBundle(_options, bundle) {
      const roots = new Set()
      for (const chunk of Object.values(bundle)) {
        if (chunk.type !== 'chunk') continue
        for (const id of Object.keys(chunk.modules)) {
          const root = packageRoot(id)
          if (root && existsSync(join(root, 'package.json'))) roots.add(root)
        }
      }

      const entries = new Map()
      const missing = []
      for (const root of roots) {
        const info = describe(root)
        const key = `${info.name}@${info.version}`
        if (entries.has(key)) continue
        let files = licenseFiles(root)
        if (!files.length && FALLBACK_TEXTS[info.name]) {
          files = [{ name: 'LICENSE', text: FALLBACK_TEXTS[info.name].trim() }]
        }
        if (!files.length) missing.push(key)
        entries.set(key, { ...info, files })
      }
      if (missing.length) {
        this.error(`以下打包进产物的依赖没有许可证全文，请在 FALLBACK_TEXTS 补原文：${missing.join(', ')}`)
      }

      const sorted = [...entries.values()].sort((a, b) => a.name.localeCompare(b.name))
      const out = [
        'SyncOJ 管理端前端产物中包含的第三方软件',
        '本文件由构建自动生成（web/scripts/license-plugin.mjs），列出打包进 web/dist 的全部第三方包及其许可证全文。',
        '',
        '清单：',
        ...sorted.map((e) => `  - ${e.name} ${e.version}（${e.license}）${e.homepage ? ' ' + e.homepage : ''}`),
        '',
      ]
      for (const e of sorted) {
        out.push('='.repeat(78), `${e.name} ${e.version}`, `许可证：${e.license}`)
        if (e.homepage) out.push(`来源：${e.homepage}`)
        out.push('-'.repeat(78))
        for (const file of e.files) {
          if (e.files.length > 1) out.push(`[${file.name}]`)
          out.push(file.text, '')
        }
      }
      this.emitFile({ type: 'asset', fileName, source: out.join('\n') + '\n' })
    },
  }
}
