/**
 * 从服务端 OpenAPI 生成的 schema 里取出的类型别名。
 *
 * **不要手写这些类型。** 它们是 `npm run gen:types` 从 `server/openapi.json`
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
export type EnrollCodeOut = S['EnrollCodeOut']

export type RosterOut = S['RosterOut']
export type RosterDetailOut = S['RosterDetailOut']
export type RosterCreate = S['RosterCreate']
export type RosterEntryIn = S['RosterEntryIn']
export type RosterEntryOut = S['RosterEntryOut']
export type RosterImportOut = S['RosterImportOut']
export type ApplyRosterIn = S['ApplyRosterIn']
export type ApplyRosterOut = S['ApplyRosterOut']

export type PendingMachineOut = S['PendingMachineOut']
export type ClaimMachineIn = S['ClaimMachineIn']
export type ClaimByCodeIn = S['ClaimByCodeIn']
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
