<#
SyncOJ 全量检查（Windows 入口）。

步骤只写在 scripts/check.py 里；这个壳和 scripts/check.sh 一样，只负责挑一个
能用的 Python，再把参数原样转过去：

    .\scripts\check.ps1                     # Windows PowerShell 5.1 与 pwsh 7 都行
    .\scripts\check.ps1 --skip-web          # 没装 node_modules 时
    .\scripts\check.ps1 --list              # 列出步骤；--only <步骤> 只跑某几步

**本文件必须带 UTF-8 BOM**：Windows PowerShell 5.1 在没有 BOM 时按 ANSI（简体中文
系统上是 GBK）解码脚本，里面的中文注释会让它整个文件解析失败。PowerShell 7 也认
BOM，所以带着两边都能跑。check.py 的 ps1-bom 一步与 test_repo_hygiene 各有一条
守卫盯着 —— 编辑工具保存时不会保留 BOM。
#>

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

# 与 check.sh 同序：先仓库里的 .venv（装着 server[dev]），再退回系统解释器。
# 可用 $env:PYTHON 指定。
$Python = $env:PYTHON
if (-not $Python) {
    $candidate = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path $candidate) { $candidate } else { "python" }
}

# 中文输出按 UTF-8 走（控制台默认 GBK 时也不会因为编码而炸）
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

& $Python (Join-Path $repoRoot "scripts\check.py") @args
exit $LASTEXITCODE
