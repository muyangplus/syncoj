<#
SyncOJ 评测机一键安装（Windows，§12.10）。服务端 GET /judge/install.ps1 发出。

在「以管理员身份运行」的 PowerShell 里：

    $p = Join-Path $env:TEMP syncoj-judge-install.ps1; iwr -UseBasicParsing http://<服务端管理端口>/judge/install.ps1 -OutFile $p; & $p -Token '<评测机令牌>'

可选参数：
    -Server URL       服务端管理端口地址（默认：发这个脚本的那台服务端）
    -Backend NAME     沙箱后端（默认 winjob）
    -Slots N          同时评几个任务（默认 1）
    -Python PATH      python.exe 路径（默认自动找 py -3 / python / python3，需要 3.8 或更新）
    -InstallDir DIR   安装目录（默认 %ProgramData%\SyncOJ\judge）

幂等：重复运行 = 换令牌 / 换参数 / 升级评测机程序，然后重启计划任务。

装到：
    <安装目录>\syncoj-judge.pyz   评测机程序（服务端按需打包的 zipapp，只用标准库）
    <安装目录>\token              令牌（只有 SYSTEM 与 Administrators 可读）
    <安装目录>\cache              数据包缓存
    <安装目录>\judge.log          日志
    计划任务「SyncOJ Judge」：开机启动、以 SYSTEM 运行、失败后每分钟重启

本文件必须保存为 UTF-8 BOM：Windows PowerShell 5.1 没有 BOM 时按 ANSI 代码页读，中文会让整个脚本解析失败。
#>
param(
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$Server = '',
    [string]$Backend = 'winjob',
    [int]$Slots = 1,
    [string]$Python = '',
    [string]$InstallDir = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 服务端发脚本时换成自己的地址与评测机程序的 sha256；从仓库里直接跑时占位符原样留着，视为没有
$EmbeddedServer = '__SYNCOJ_SERVER__'
$PyzSha256 = '__SYNCOJ_PYZ_SHA256__'
if ($EmbeddedServer -like '__SYNCOJ_*__') { $EmbeddedServer = '' }
if ($PyzSha256 -like '__SYNCOJ_*__') { $PyzSha256 = '' }

$TaskName = 'SyncOJ Judge'

function Fail([string]$Message) {
    Write-Host "错误：$Message" -ForegroundColor Red
    exit 1
}
function Say([string]$Message) { Write-Host "==> $Message" }
function Warn([string]$Message) { Write-Host "!!  $Message" -ForegroundColor Yellow }

if (-not $Server) { $Server = $EmbeddedServer }
if ($Token -notmatch '^[A-Za-z0-9_-]+$') { Fail '令牌只含字母、数字、- 和 _（抄错了？）' }
if (-not $Server) { Fail '缺少服务端地址：加 -Server http://<服务端>:8000' }
$Server = $Server.TrimEnd('/')
if ($Slots -lt 1) { Fail '-Slots 要是正整数' }
if ($Backend -notmatch '^[a-z0-9]+$') { Fail "后端名不合法：$Backend" }

$identity = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail '需要管理员权限：右键「以管理员身份运行」PowerShell 后重试'
}
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData 'SyncOJ\judge' }

# ---- Python（3.8 或更新）----
function Find-Python {
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    else { $candidates += , @('py', '-3'); $candidates += , @('python'); $candidates += , @('python3') }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $prefix = @($candidate | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $lines = @(& $exe @prefix -c 'import sys; print(sys.executable); print(1 if sys.version_info[:2] >= (3, 8) else 0)' 2>$null)
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or $lines.Count -lt 2) { continue }
        if ($lines[1].Trim() -ne '1') { Warn "$exe 版本太旧，需要 3.8 或更新"; continue }
        $path = $lines[0].Trim()
        if ($path -and (Test-Path $path)) { return $path }
    }
    return $null
}
$PythonExe = Find-Python
if (-not $PythonExe) { Fail '没有找到 Python 3.8 或更新（https://www.python.org/downloads/windows/，安装时勾选 Add to PATH）' }
Say "Python：$PythonExe"

# ---- 编译器（MinGW g++）----
$gpp = Get-Command g++ -ErrorAction SilentlyContinue
if (-not $gpp) {
    Warn '没有 g++（MinGW-w64）：评测会全部记为系统错误。装好后把它的 bin 目录加进「系统」PATH'
} else {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $gppDir = Split-Path -Parent $gpp.Source
    if (-not (($machinePath -split ';') | Where-Object { $_.TrimEnd('\') -ieq $gppDir.TrimEnd('\') })) {
        Warn "g++ 在 $gppDir，但它不在「系统」PATH 里；计划任务以 SYSTEM 运行，找不到它"
    } else {
        Say "g++：$($gpp.Source)"
    }
}

# ---- 程序 ----
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$CacheDir = Join-Path $InstallDir 'cache'
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
$Pyz = Join-Path $InstallDir 'syncoj-judge.pyz'
$Tmp = "$Pyz.download"
Say "下载评测机程序：$Server/judge/worker.pyz"
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$Server/judge/worker.pyz" -OutFile $Tmp
} catch {
    Fail "下载失败：$Server/judge/worker.pyz（$($_.Exception.Message)）"
}
if ($PyzSha256) {
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Tmp).Hash.ToLower()
    if ($actual -ne $PyzSha256) { Warn "评测机程序的 sha256 与脚本里记的不同（服务端刚升级过？）：$actual" }
}
& $PythonExe $Tmp --help | Out-Null
if ($LASTEXITCODE -ne 0) { Remove-Item -Force $Tmp; Fail '下载的评测机程序跑不起来' }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}
Move-Item -Force -Path $Tmp -Destination $Pyz

# ---- 令牌：只有 SYSTEM 与 Administrators 可读（按 SID 授权，不依赖系统语言）----
$TokenFile = Join-Path $InstallDir 'token'
Set-Content -Path $TokenFile -Value $Token -Encoding Ascii
& icacls.exe $TokenFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { Warn '令牌文件权限没有收紧（icacls 失败）' }

# ---- 计划任务 ----
$LogFile = Join-Path $InstallDir 'judge.log'
$arguments = @(
    "`"$Pyz`"",
    '--server', $Server,
    '--token-file', "`"$TokenFile`"",
    '--backend', $Backend,
    '--slots', $Slots,
    '--cache-dir', "`"$CacheDir`"",
    '--log-file', "`"$LogFile`""
) -join ' '
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $arguments -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'SyncOJ judge worker' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ("$state" -eq 'Running') {
    Say "评测机已启动：后端 $Backend，并发 $Slots，服务端 $Server"
    Say "日志：$LogFile"
} else {
    Warn "计划任务状态：$state"
    if (Test-Path $LogFile) { Get-Content -Tail 20 $LogFile }
    Fail '评测机没有起来，见上面的日志'
}
