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
export type UpgradeStatusOut = S['UpgradeStatusOut']

export type SimpleAck = S['SimpleAck']

/** 成绩单元格的解析状态。定义在后端 `models.JudgeRun.parse_status`。 */
export type ParseStatus = 'ok' | 'unparsed' | 'manual' | 'missing'

/** 下发目标的进度状态。定义在后端 `models.DeployStatus`。 */
export type DeployStatus = 'pending' | 'ready' | 'done' | 'failed' | 'cancelled'

/** 场次状态。定义在后端 `models.ContestStatus`。 */
export type ContestStatus = 'draft' | 'running' | 'frozen' | 'closed'
