/**
 * 从服务端 OpenAPI 生成的 schema 里取出的类型别名。
 *
 * **不要手写这些类型。** 它们是 `npm run gen:types` 从 `web/openapi.json`
 * 生成的，而后者又由 `server/tools/dump_openapi.py` 从 pydantic 模型导出。
 * 服务端模型是唯一真相源；这里只是给它起个短名字。
 */

import type { components } from './schema'

type S = components['schemas']

export type AdminInfo = S['AdminInfo']
export type LoginRequest = S['LoginRequest']
export type LoginResponse = S['LoginResponse']

export type ContestOut = S['ContestOut']
export type ContestCreate = S['ContestCreate']
export type ContestUpdate = S['ContestUpdate']
export type PlayerOut = S['PlayerOut']
export type PlayerUpsert = S['PlayerUpsert']
/**
 * 批量导入选手的结果。**不是列表信封** —— 信封只属于 GET 集合，
 * 这是动作结果，`players` 让界面拿到新建行的 id。
 */
export type PlayerImportOut = S['PlayerImportOut']

export type RosterOut = S['RosterOut']
export type RosterDetailOut = S['RosterDetailOut']
export type RosterCreate = S['RosterCreate']
export type RosterEntryIn = S['RosterEntryIn']
export type RosterEntryOut = S['RosterEntryOut']
export type RosterImportOut = S['RosterImportOut']
export type ApplyRosterIn = S['ApplyRosterIn']
export type ApplyRosterOut = S['ApplyRosterOut']

export type PendingMachineOut = S['PendingMachineOut']
/** 按机器配对。`pair_code` 可省，但给了就必须对得上。 */
export type BindMachineIn = S['BindMachineIn']
/** 按配对码配对。六位数字，只在绑定的那一刻有效。 */
export type BindByCodeIn = S['BindByCodeIn']
/** 改派给名单里的另一个人。 */
export type RebindAgentIn = S['RebindAgentIn']
/** 指定/取消指定场次。`contest_id=null` 回到动态解析。 */
export type SetAgentContestIn = S['SetAgentContestIn']
/** 配对成功的回执。回显"绑到了谁、在哪份名单里"。 */
export type BindResultOut = S['BindResultOut']
export type CloneAlertOut = S['CloneAlertOut']
export type BootstrapKeyOut = S['BootstrapKeyOut']
export type BootstrapKeyIssuedOut = S['BootstrapKeyIssuedOut']
export type BootstrapKeyIssueIn = S['BootstrapKeyIssueIn']

export type ProblemOut = S['ProblemOut']
export type ProblemUpsert = S['ProblemUpsert']
export type ProblemImportOut = S['ProblemImportOut']
export type ProblemMatchIn = S['ProblemMatchIn']
export type ProblemMatchOut = S['ProblemMatchOut']

export type AgentRuntimeOut = S['AgentRuntimeOut']
export type SourceFileOut = S['SourceFileOut']
export type EventOut = S['EventOut']

export type AssetOut = S['AssetOut']
/** 只改显示名。内容是按 sha256 存的，改名不触碰到内容。 */
export type AssetRenameIn = S['AssetRenameIn']
/** 一个可在线编辑的纯文本资产的正文（GET 读）。回显 `filename` 给对话框写标题。 */
export type AssetTextOut = S['AssetTextOut']
/** 在线改正文。只有内容 —— 文件名不在这个入口里（改名是另一个动作）。 */
export type AssetTextEditIn = S['AssetTextEditIn']
/** 改正文的回执：`asset` 是改后的那一份，`requeued` 是被重排的机器台数。 */
export type AssetTextSavedOut = S['AssetTextSavedOut']
/** zip 的加密状态。GET 只读标志位（不解压、不解密）—— 不是 zip 会拿到 400。 */
export type AssetZipPasswordOut = S['AssetZipPasswordOut']
/**
 * 给 zip 打密码/改密码。已经加密时必须给 `old_password`；资产本来不是 zip 时，
 * 服务端会先把它打包成 zip（同一个 asset id，文件名换成 `<原基名>.zip`）。
 */
export type AssetZipPasswordIn = S['AssetZipPasswordIn']
/**
 * 回执：新密码、重新打包后的资产、以及同步写好的 password.txt。
 * `packaged` 区分这次是"打包成 zip"还是"给已有 zip 改密码"。
 */
export type AssetZipPasswordSavedOut = S['AssetZipPasswordSavedOut']
export type DeployCreate = S['DeployCreate']
export type DeployTaskOut = S['DeployTaskOut']
export type DeployTargetOut = S['DeployTargetOut']

export type ScoreMatrixOut = S['ScoreMatrixOut']
export type ScoreRowOut = S['ScoreRowOut']
export type ScoreCellOut = S['ScoreCellOut']
export type ProblemColumnOut = S['ProblemColumnOut']
export type JudgeRunOut = S['JudgeRunOut']
export type JudgeScanOut = S['JudgeScanOut']
export type ManualScoreIn = S['ManualScoreIn']

export type ReleaseOut = S['ReleaseOut']
export type ReleaseUpdate = S['ReleaseUpdate']
export type ReleaseSourceOut = S['ReleaseSourceOut']
export type InstallLedgerOut = S['InstallLedgerOut']
export type UpgradeStatusOut = S['UpgradeStatusOut']

export type SimpleAck = S['SimpleAck']

/** 全站运行时常量（`GET /api/v1/meta`）。免登录，三个前台页面都读它。 */
export type MetaOut = S['MetaOut']

/** 运行参数：心跳节奏与离线判定。整间机房生效，改完立即生效。 */
export type RuntimeSettingsOut = S['RuntimeSettingsOut']
export type RuntimeSettingsUpdate = S['RuntimeSettingsUpdate']

/** 成绩单元格的解析状态。定义在后端 `models.JudgeRun.parse_status`。 */
export type ParseStatus = 'ok' | 'unparsed' | 'manual' | 'missing'

/** 下发目标的进度状态。定义在后端 `models.DeployStatus`。 */
export type DeployStatus = 'pending' | 'ready' | 'done' | 'failed' | 'cancelled'

/** 场次状态。定义在后端 `models.ContestStatus`。 */
export type ContestStatus = 'draft' | 'running' | 'frozen' | 'closed'
