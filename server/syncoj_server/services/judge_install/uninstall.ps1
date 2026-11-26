<#
SyncOJ 评测机卸载（Windows，§12.10）。install.ps1 把它写到安装目录（默认 %ProgramData%\SyncOJ\judge\uninstall.ps1），
只有 SYSTEM 与 Administrators 能改。

手动卸载（幂等）：在「以管理员身份运行」的 PowerShell 里

    & "$env:ProgramData\SyncOJ\judge\uninstall.ps1"

-IfRequested：计划任务「SyncOJ Judge Cleanup」（SYSTEM）用。评测机告别服务端后在 cache\ 里写了标记
才动手；标记只看在不在，不读内容。

删除：计划任务「SyncOJ Judge」与「SyncOJ Judge Cleanup」、整个安装目录（程序、令牌、缓存、工作目录、日志）。
不动 Python 与 MinGW。

本文件必须保存为 UTF-8 BOM（Windows PowerShell 5.1 没有 BOM 时按 ANSI 代码页读）。
#>
param(
    [switch]$IfRequested
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$TaskName = 'SyncOJ Judge'
$CleanupTaskName = 'SyncOJ Judge Cleanup'
# 安装目录就是本脚本所在的目录：不收参数，免得调用方把它指到别处
$InstallDir = $PSScriptRoot
$Marker = Join-Path $InstallDir 'cache\.uninstall-requested'

function Say([string]$Message) { Write-Host "==> $Message" }
function Warn([string]$Message) { Write-Host "!!  $Message" -ForegroundColor Yellow }

if ($IfRequested -and -not (Test-Path -LiteralPath $Marker)) { exit 0 }

# 只删真正的评测机安装目录：脚本被复制到别处（下载目录、仓库检出）运行时，这里的整目录删除
# 会删错地方。安装目录里必有 install.ps1 写的 install.json（kind = schtask）与评测机程序
$ManifestPath = Join-Path $InstallDir 'install.json'
$IsInstallDir = $false
if ((Test-Path -LiteralPath $ManifestPath) -and (Test-Path -LiteralPath (Join-Path $InstallDir 'syncoj-judge.pyz'))) {
    try {
        $IsInstallDir = ((Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json).kind -eq 'schtask')
    } catch { $IsInstallDir = $false }
}
if (-not $IsInstallDir) {
    Write-Host "错误：$InstallDir 不是评测机的安装目录（没有 install.json / syncoj-judge.pyz）：请运行安装目录里的 uninstall.ps1" -ForegroundColor Red
    exit 1
}

$identity = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '错误：需要管理员权限：右键「以管理员身份运行」PowerShell 后重试' -ForegroundColor Red
    exit 1
}

# ---- 评测机计划任务：停掉（连同它的 python 进程）并删除 ----
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Say "已删除计划任务「$TaskName」"
}
Start-Sleep -Seconds 2

# ---- 安装目录：rmdir /s 不跟随目录联接（junction），Remove-Item -Recurse 在 PowerShell 5.1 上会 ----
Set-Location -LiteralPath $env:SystemRoot
if (Test-Path -LiteralPath $InstallDir) {
    & cmd.exe /d /c "rmdir /s /q `"$InstallDir`"" 2>$null
    if (Test-Path -LiteralPath $InstallDir) {
        Start-Sleep -Seconds 3
        & cmd.exe /d /c "rmdir /s /q `"$InstallDir`"" 2>$null
    }
    if (Test-Path -LiteralPath $InstallDir) { Warn "没能删干净 $InstallDir（文件被占用？）：稍后手动删除" }
    else { Say "已删除 $InstallDir" }
}

# ---- 最后删清理任务自己（多半正以它运行；删掉不影响本次运行做完）----
if (Get-ScheduledTask -TaskName $CleanupTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $CleanupTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Say "已删除计划任务「$CleanupTaskName」"
}
Say '评测机已卸载'
exit 0
