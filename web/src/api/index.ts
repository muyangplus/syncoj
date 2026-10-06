/**
 * 管理端接口的**入口**：每个资源一个模块，页面只从这里 import。
 *
 * 这一层只做「路径 + 参数 + 返回类型」的映射，业务逻辑不放这里（那属于页面
 * 或 store）。分页、信封、`confirm`、错误体全部由 `crud.ts` / `endpoints.ts`
 * 吃掉 —— 所以拆开之后每个资源模块都很薄，没有共享状态，也没有相互依赖。
 *
 * **列表方法统一是 `(key, params?, signal?)`**：`key` 是父资源标识（大多数
 * 情况下是场次 id），`params` 里放分页与筛选，`signal` 用来取消过期请求。
 * 形状统一的好处是页面里那句 `useList` 的 loader 写到哪里都一样。
 */

export { authApi } from './auth'
export type { AdminHealth } from './auth'
export { contestApi } from './contests'
export { rosterApi } from './rosters'
export { playerApi } from './players'
export { problemApi } from './problems'
export { agentApi } from './agents'
export { machineApi } from './machines'
export { bootstrapKeyApi } from './bootstrapKeys'
export { fileApi } from './files'
export { eventApi } from './events'
export { assetApi } from './assets'
export { deployApi } from './deploys'
export { scoreApi } from './scores'
export { releaseApi, CONFIG_POLICY_CHOICES, UPGRADE_MODE_CHOICES } from './releases'
export type { InstallPolicy } from './releases'
export { installApi, absolute, bootstrapCommand, uninstallCommand } from './install'
export { metaApi } from './meta'
export { settingsApi } from './settings'
export { playerPageApi } from './playerPage'
export type { PlayerAssetOut, PlayerContextOut, PlayerNoticeOut } from './playerPage'

// 公共类型也在这里转出去：页面不该关心它实际定义在 crud.ts 还是别处
export type { ListParams } from './crud'
