<#
SyncOJ 全量检查（Windows 开发机版）。

`scripts/check.sh` 是权威入口，两个脚本**步骤一一对应**。

为什么还留一个 .ps1：Windows 上 bash 只随 Git 一起出现（`D:\Program Files\Git\bin\bash.exe`），
它跑 check.sh 是可行的 —— msys 会把 `PYTHONPATH` 翻译成 Windows 路径，实测能导入
`syncoj_server`。但那条路要跨一层 msys 的路径翻译，出问题时错误信息会指向奇怪的地方；
直接调 Windows 上的 PowerShell 更接近"到底发生了什么"。日常在 Windows 上用这个，
CI 与 Linux 上用 check.sh。

**本文件必须带 UTF-8 BOM**：Windows PowerShell 5.1 在没有 BOM 时按 ANSI（简体中文
系统上是 GBK）解码脚本，下面的中文注释会让它整个文件解析失败。PowerShell 7 也认
BOM，所以带着两边都能跑。`check.sh` 与 `test_repo_hygiene.py` 各有一条守卫盯着这件事 ——
编辑工具保存时不会保留 BOM，改一行注释就可能把它弄丢。

改了其中一个就要改另一个，否则两个平台给出的"通过"含义不同，那比只有一个入口更糟。

    .\scripts\check.ps1                      # Windows PowerShell 5.1 与 pwsh 7 都行
    .\scripts\check.ps1 -SkipWeb             # 没装 node_modules 时
#>

param(
    # 默认用仓库根目录的 .venv；没有就退回 PATH 上的 python
    [string]$Python = "",
    [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not $Python) {
    $candidate = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path $candidate) { $candidate } else { "python" }
}

# 测试不许往 E:\var\lib\syncoj 写 —— 那是真机路径，出现在开发机上只会让人
# 以为自己装了一份服务端。所有测试都读这个环境变量决定状态目录。
$env:SYNCOJ_STATE_DIR = Join-Path $repoRoot ".tmp-state"
# 强制子进程的 stdin/stdout 用 UTF-8。不能靠环境：Windows 上子进程默认按控制台
# 代码页（GBK）写中文，而 `test_agent_contract.py` 用 utf-8 解码 Agent 生成的
# JSON 样本，会当场抛 UnicodeDecodeError。这是"进程间约定"问题，两边都钉死。
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force -Path $env:SYNCOJ_STATE_DIR | Out-Null

function Step([string]$name) {
    Write-Host ""
    Write-Host "==> $name" -ForegroundColor Cyan
}

# 外部命令非零退出不会抛异常，必须显式检查 —— 否则 `npm run build` 失败之后
# 脚本会一路跑到底，最后打印"全部检查通过"。
function Assert-Ok([string]$name) {
    if ($LASTEXITCODE -ne 0) {
        throw "$name 失败（exit $LASTEXITCODE）"
    }
}

Step "Python 3.8 兼容门禁（Agent 必须能跑在 NOI Linux 的 Python 3.8 上）"
& $Python agent/tools/check_py38.py agent/
Assert-Ok "check_py38 agent/"
# 服务端也一并检查。它自己有第三方依赖，用 --allow 显式列出，
# 而不是把 fastapi/sqlalchemy 混进标准库白名单里。
& $Python agent/tools/check_py38.py `
    --allow fastapi,sqlalchemy,pydantic,starlette,uvicorn `
    server/syncoj_server/
Assert-Ok "check_py38 server/"

# 临时目录收进仓库里已忽略的 .pytest-tmp/，而不是散在系统的 %TEMP% 里。
#
# 曾经这里写的是 `-p no:tmpdir`，那把 `tmp_path` **整个夹具**一起禁掉了 ——
# 任何用到它的测试都会在 setup 阶段报 "fixture 'tmp_path' not found"，
# 而报错看起来像"测试写错了"，不像"脚本禁掉了标准夹具"。收临时目录用
# --basetemp 就够了，不需要把整个插件关掉。
#
# basetemp 与两个 conftest 自己的 workdir 根**必须是不同的目录**：它们曾经是
# 同一个，于是 pytest 在第一次用到 `tmp_path` 时会清空整个 basetemp —— 那时里面
# 已经有本次会话里别的测试留下的临时目录，其中 `state/agent.log` 还被日志处理器
# 占着。表现是 Windows 上一串 "PermissionError: 另一个程序正在使用此文件"。
$pytestTmp = Join-Path $repoRoot ".pytest-tmp"
$pytestBase = Join-Path $pytestTmp "basetemp"
New-Item -ItemType Directory -Force -Path $pytestBase | Out-Null

Step "PowerShell 脚本的编码"
# 仓库里的 .ps1 都带中文注释，而 Windows PowerShell 5.1 在**没有 BOM** 时按当前
# ANSI 代码页（简体中文系统上是 GBK）解码脚本文件：于是那些中文注释会让它整个脚本
# **解析失败**，报的还是"字符串缺少终止符"这种指向别处的错。PS 7 认 BOM，所以带上
# BOM 两边都对。
#
# 为什么值得一步守卫：编辑类工具保存时**不会保留 BOM**，改一行中文注释就把它弄丢了，
# 而症状（换一台只有 PS 5.1 的机器才发现脚本跑不起来）跟那次改动毫无关系。
foreach ($script in Get-ChildItem (Join-Path $repoRoot "scripts\*.ps1")) {
    $bytes = [System.IO.File]::ReadAllBytes($script.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if (-not $hasBom) {
        throw "$($script.FullName) 缺少 UTF-8 BOM：Windows PowerShell 5.1 会把它的中文按 ANSI 解码，整个脚本解析失败。修法：在文件最前面补上 EF BB BF 这三个字节。"
    }
}

Step "Agent 测试"
# 按文件并行跑（server/tools/parallel_tests.py，零依赖）：服务端那套串行要 40 分钟，
# 而它慢在"每个用例都造一次真 app + 真库 + 真 HTTP"，不在测试逻辑本身 ——
# 按文件分片就能把核用起来。worker 数用 SYNCOJ_TEST_JOBS 覆盖（默认 min(核数, 8)）。
$jobs = if ($env:SYNCOJ_TEST_JOBS) { $env:SYNCOJ_TEST_JOBS } else { "8" }
& $Python (Join-Path $repoRoot "server\tools\parallel_tests.py") --root agent --jobs $jobs
Assert-Ok "agent pytest"

Step "服务端测试（含协议契约测试与端到端集成测试）"
& $Python (Join-Path $repoRoot "server\tools\parallel_tests.py") --root server --jobs $jobs
Assert-Ok "server pytest"

Step "OpenAPI 与前端类型是否同步"
# 服务端 schema 改了但 web/openapi.json 没重新生成时，这里会拦住。
# 否则前端会在运行期才发现字段对不上，而那时已经很难定位。
& $Python server/tools/dump_openapi.py --check
Assert-Ok "dump_openapi --check"

$webReady = -not $SkipWeb -and (Test-Path (Join-Path $repoRoot "web\node_modules")) -and `
    ($null -ne (Get-Command npm -ErrorAction SilentlyContinue))

if ($webReady) {
    Step "前端类型是否已从 openapi.json 重新生成"
    # 判据是"重新生成一遍，结果和仓库里的那份是不是一致" —— **不要用 git diff**。
    #
    # git diff 比的是工作树和 HEAD，而开发中工作树本来就有未提交的改动，于是这条
    # 检查在"你正在干活"这个最常见的场景下恒红。假警报比没有检查更糟：它会训练
    # 人无视这一步，真的不同步时也就没人看了。
    $schemaPath = Join-Path $repoRoot "web\src\api\schema.d.ts"
    $schemaBefore = Join-Path $pytestTmp "schema.d.ts.before"
    Copy-Item $schemaPath $schemaBefore -Force
    Push-Location web
    try {
        npm run gen:types
        Assert-Ok "npm run gen:types"
    } finally {
        Pop-Location
    }
    if ((Get-FileHash $schemaBefore).Hash -ne (Get-FileHash $schemaPath).Hash) {
        throw "web/src/api/schema.d.ts 与 web/openapi.json 不同步：重新生成的结果和仓库里的那份不一样。请把刚重新生成的 schema.d.ts 一起提交。"
    }

    Step "前端入口对账、类型检查与构建"
    # npm run build 已经串了 check:routes → typecheck → vite build，
    # 所以这一条同时守住"声明的接口都有界面入口"。
    Push-Location web
    try {
        npm run typecheck
        Assert-Ok "npm run typecheck"
        npm run build
        Assert-Ok "npm run build"
    } finally {
        Pop-Location
    }
} else {
    Step "跳过前端检查（未安装 npm 或 web/node_modules 不存在）"
}

Step "全部检查通过"
