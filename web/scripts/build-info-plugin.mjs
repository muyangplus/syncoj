/**
 * 构建时写出 dist/build-info.json：{version, source_sha256, built_at}。
 *
 * 服务端启动时（server/syncoj_server/webdist.py）按同一算法重算前端源码指纹，
 * 对不上就提示「前端构建已过期」。算法两边必须一致（server/tests/test_webdist.py 对拍）：
 * 取 SOURCE_FILES 与 SOURCE_DIRS 下全部文件（跳过以 . 开头的名字），按相对路径排序；
 * 每个文件把 \r\n 换成 \n 后算 sha256，拼成 `<路径>\0<十六进制摘要>\n`，对整串再算 sha256。
 */

import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

export const SOURCE_FILES = ['index.html', 'package.json', 'package-lock.json', 'vite.config.ts', 'tsconfig.json', 'env.d.ts']
export const SOURCE_DIRS = ['src', 'public', 'scripts']

function walk(dir, out) {
  for (const name of readdirSync(dir)) {
    if (name.startsWith('.')) continue
    const full = join(dir, name)
    const st = statSync(full)
    if (st.isDirectory()) walk(full, out)
    else if (st.isFile()) out.push(full)
  }
}

/** 前端源码指纹。 */
export function sourceSha256(webRoot) {
  const files = []
  for (const name of SOURCE_FILES) {
    const full = join(webRoot, name)
    if (existsSync(full) && statSync(full).isFile()) files.push(name)
  }
  for (const folder of SOURCE_DIRS) {
    const base = join(webRoot, folder)
    if (!existsSync(base) || !statSync(base).isDirectory()) continue
    const found = []
    walk(base, found)
    for (const full of found) files.push(relative(webRoot, full).split(sep).join('/'))
  }
  // 按 UTF-16 码元排序与 Python 的按码点排序在 BMP 内一致；路径都是 ASCII
  files.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
  const outer = createHash('sha256')
  for (const rel of files) {
    const text = readFileSync(join(webRoot, rel))
    const data = Buffer.from(text.toString('latin1').replace(/\r\n/g, '\n'), 'latin1')
    const inner = createHash('sha256').update(data).digest('hex')
    outer.update(Buffer.concat([Buffer.from(rel, 'utf8'), Buffer.from([0]), Buffer.from(inner + '\n', 'ascii')]))
  }
  return outer.digest('hex')
}

export function buildInfo({ root, fileName = 'build-info.json' }) {
  return {
    name: 'syncoj-build-info',
    apply: 'build',
    generateBundle() {
      const { version } = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))
      const info = { version, source_sha256: sourceSha256(root), built_at: new Date().toISOString() }
      this.emitFile({ type: 'asset', fileName, source: JSON.stringify(info, null, 2) + '\n' })
    },
  }
}

// 命令行：node scripts/build-info-plugin.mjs <webRoot> → 打印指纹（测试对拍用）
if (process.argv[1] && process.argv[1].endsWith('build-info-plugin.mjs') && process.argv[2]) {
  process.stdout.write(sourceSha256(process.argv[2]) + '\n')
}
