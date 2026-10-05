/**
 * 前端入口对账。
 *
 * 用户的原话是「web端很多功能都没有编辑/删除/清空入口」。那不是一次失误，而是
 * 一种会**反复出现**的失误：服务端加了接口，前端忘了接；或者路径改了名，前端
 * 还在调旧的，界面上表现为"点了没反应"。靠人记得去核对是防不住的，所以把它
 * 变成一条能跑的命令。
 *
 * 检查两件事：
 *
 * 1. **前端写下的每条路径，服务端都认**
 *    —— 对着 `openapi.json`（由 `server/tools/dump_openapi.py` 生成）比。
 *    路径打错/接口被改名会在构建前就红，而不是等到教师点按钮。
 *
 * 2. **`api/index.ts` 里声明的每个方法，都有人调**
 *    —— 在整个 `src/`（排除 `api/` 自己）里搜调用点。没有任何调用者的方法
 *    意味着"服务端有这个能力，但界面上进不去"。少数确实不该有入口的（比如
 *    内部组合用的方法）写进 `ALLOWED_UNUSED` 并说明理由。
 *
 * 用法:
 *
 *     npm run check:routes
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SRC = join(WEB_ROOT, 'src')

/**
 * 允许"没有界面入口"的方法。
 *
 * 每一项都要有理由 —— 这个清单本身是"我们确实想过这件事"的记录，
 * 而不是让检查闭嘴的开关。
 */
const ALLOWED_UNUSED = new Map([
  [
    'deployApi.get',
    '单任务详情。下发任务的列表接口已经用 include_targets=true 一次带回逐选手进度，' +
      '所以界面上没有"再查一遍这一个任务"的必要 —— 留着它是给将来的任务详情页用；' +
      '真接了那个页面就把这条删掉',
  ],
  [
    'releaseApi.update',
    '只改备注/通道。备注编辑走的是另一个按钮，若将来拆出去要把它从这里删掉',
  ],
])

/**
 * 把 `/contests/{contest_id}/players` 与 `/contests/${x}/players` 归一成同一个形状。
 *
 * **顺序不能反**：先换 `${...}`。反过来的话 `\{[^}]+\}` 会先咬掉 `${contestId}`
 * 里层的 `{contestId}`，结果是 `${}` —— 一个既不像前端写法也不像服务端写法的
 * 东西，于是每一条带参数的路由都会误报成"服务端没有"。
 */
function normalize(path) {
  return path.replace(/\$\{[^}]+\}/g, '{}').replace(/\{[^}]+\}/g, '{}')
}

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}

/** `endpoints.ts` 里所有 `` `${ADMIN}...` `` 模板串，归一成 `{}` 形状。 */
function frontendPaths() {
  const text = readFileSync(join(SRC, 'api', 'endpoints.ts'), 'utf8')
  const found = new Map()
  for (const line of text.split('\n')) {
    for (const match of line.matchAll(/`(\$\{ADMIN\}[^`]*)`/g)) {
      const path = normalize(match[1].replace('${ADMIN}', '/api/v1/admin'))
      found.set(path, line.trim())
    }
  }
  return found
}

function serverPaths() {
  const spec = JSON.parse(readFileSync(join(WEB_ROOT, 'openapi.json'), 'utf8'))
  return new Set(Object.keys(spec.paths ?? {}).map(normalize))
}

/**
 * `api/index.ts` 里 `export const xApi = {` 下的方法名 → `xApi.method`。
 *
 * 靠**缩进**认方法：每个资源的 `export const xApi = {` 在顶层，它的方法固定在
 * 两格缩进上，方法体更深。不做通用 AST 解析是有意的 —— 这里要的是"能挡住
 * 漏接接口"，不是"能解析任意 TS"；而通用解析的复杂度会让人不敢改它。
 *
 * 代价是它对格式化敏感，所以下面有一个**条数下限**：一旦被重新格式化导致
 * 一条都认不出来，检查会红，而不是静默通过（"什么都没找到"等于"全部通过"
 * 是这类检查最典型的死法）。
 */
const MIN_EXPECTED_METHODS = 40

function declaredMethods() {
  const text = readFileSync(join(SRC, 'api', 'index.ts'), 'utf8')
  const found = []
  let current = null
  for (const line of text.split('\n')) {
    const open = /^export const (\w+) = \{$/.exec(line)
    if (open) {
      current = open[1]
      continue
    }
    if (line === '}') {
      current = null
      continue
    }
    if (current === null) continue
    const method = /^ {2}(\w+):\s*(?:async\s*)?\(/.exec(line)
    if (method) found.push(`${current}.${method[1]}`)
  }
  if (found.length < MIN_EXPECTED_METHODS) {
    console.error(
      `入口对账自身失效：只从 api/index.ts 里认出 ${found.length} 个方法` +
        `（预期至少 ${MIN_EXPECTED_METHODS} 个）。` +
        '多半是文件被重新格式化、缩进变了 —— 改这个脚本的匹配规则，而不是放宽下限。',
    )
    process.exit(1)
  }
  return found
}

/** `src/` 里除了 `api/` 之外的所有调用点。 */
function callSites() {
  const files = walk(SRC).filter(
    (file) => !file.includes(`${join('src', 'api')}`) && !file.endsWith('.d.ts'),
  )
  return files.map((file) => ({
    file: file.replace(WEB_ROOT, '').replace(/\\/g, '/'),
    text: readFileSync(file, 'utf8'),
  }))
}

const problems = []

const declaredPaths = frontendPaths()
const known = serverPaths()
for (const [path, line] of declaredPaths) {
  if (!known.has(path)) {
    problems.push(`前端写了服务端没有的路径：${path}\n      ${line}`)
  }
}

const sites = callSites()
for (const method of declaredMethods()) {
  if (sites.some((site) => site.text.includes(method))) continue
  if (ALLOWED_UNUSED.has(method)) continue
  problems.push(
    `这个接口没有任何界面入口：${method}\n` +
      '      （服务端有、前端声明了，但 src/views|components|stores|composables 里没人调它）',
  )
}

// 只看管理端：Agent 侧的路径（`/api/v1/agent/*`）与探活 `/healthz` 不归这个
// 界面管，把它们列出来只会让这份提示变成一眼扫过就忽略的噪音。
const unusedByServer = [...known].filter(
  (path) => path.startsWith('/api/v1/admin') && !path.includes('{') && !declaredPaths.has(path),
)

if (problems.length) {
  console.error('入口对账没通过：\n')
  for (const problem of problems) console.error(`  ✗ ${problem}`)
  console.error(`\n共 ${problems.length} 处。`)
  process.exit(1)
}

console.log(
  `入口对账通过：前端 ${declaredPaths.size} 条路径服务端都认，` +
    `声明的接口都有界面入口。`,
)
if (unusedByServer.length) {
  // 只提示，不算失败：这些可能是 Agent 侧、探活、或前端故意不做的
  console.log(`提示：服务端还有 ${unusedByServer.length} 条路径前端没写：`)
  for (const path of unusedByServer) console.log(`  · ${path}`)
}
