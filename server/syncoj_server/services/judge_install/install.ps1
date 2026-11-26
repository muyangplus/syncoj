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
    <安装目录>\token              令牌（SYSTEM、Administrators、LOCAL SERVICE 可读；低完整性进程不可读）
    <安装目录>\cache              数据包缓存（LOCAL SERVICE 可改；低完整性进程不可读）
    <安装目录>\boxes              评测工作目录（LOCAL SERVICE 可改）
    <安装目录>\judge.log          日志
    <安装目录>\uninstall.ps1      卸载脚本（只有 SYSTEM、Administrators 能改）：管理员运行即卸载
    <安装目录>\install.json       安装方式（评测机远程卸载时据此清理自己）
    计划任务「SyncOJ Judge」：开机启动、以 LOCAL SERVICE 运行（不是 SYSTEM）、失败后每分钟重启
    计划任务「SyncOJ Judge Cleanup」：以 SYSTEM 运行，开机与每 2 分钟看一眼 cache\.uninstall-requested，
        在（评测机收到远程卸载、告别服务端后写的）才执行 uninstall.ps1；标记只看在不在，不读内容

安全：评测机以 LOCAL SERVICE 运行；选手程序与 checker 再降为受限令牌 + 低完整性级别，
读不了令牌与数据包缓存（NO_READ_UP 标签）、写不了工作目录以外、读不了评测机进程内存。不隔离网络。
因此 Python 与 g++ 都要装在 LOCAL SERVICE 能读的位置（Python 选「为所有用户安装」），g++ 在「系统」PATH。

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
$CleanupTaskName = 'SyncOJ Judge Cleanup'
# 按 SID 授权与指定账号，不依赖系统语言
$LocalServiceSid = 'S-1-5-19'
$LocalServiceSidObject = New-Object Security.Principal.SecurityIdentifier $LocalServiceSid
$LocalService = $LocalServiceSidObject.Translate([Security.Principal.NTAccount]).Value

function Fail([string]$Message) {
    Write-Host "错误：$Message" -ForegroundColor Red
    exit 1
}
function Say([string]$Message) { Write-Host "==> $Message" }
function Warn([string]$Message) { Write-Host "!!  $Message" -ForegroundColor Yellow }

# LOCAL SERVICE 能不能读这个文件：看 ACL 里有没有给 LOCAL SERVICE / Users / Authenticated Users / Everyone 读与执行
function Test-LocalServiceCanRead([string]$Path) {
    $sids = @($LocalServiceSid, 'S-1-5-32-545', 'S-1-5-11', 'S-1-1-0')
    $need = [Security.AccessControl.FileSystemRights]::ReadAndExecute
    try { $acl = Get-Acl -LiteralPath $Path } catch { return $false }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        try { $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { continue }
        if (($sids -contains $sid) -and (($rule.FileSystemRights -band $need) -eq $need)) { return $true }
    }
    return $false
}

if (-not $Server) { $Server = $EmbeddedServer }
if ($Token -notmatch '^[A-Za-z0-9_-]+$') { Fail '令牌只含字母、数字、- 和 _（抄错了？）' }
if (-not $Server) { Fail '缺少服务端地址：加 -Server http://<服务端>:8000' }
$Server = $Server.TrimEnd('/')
if ($Slots -lt 1) { Fail '-Slots 要是正整数' }
if ($Slots -gt 1) { Fail '-Slots 大于 1 只能用于 Linux 的 isolate（winjob 的并发任务能互相读写工作目录）' }
if ($Backend -notmatch '^[a-z0-9]+$') { Fail "后端名不合法：$Backend" }

$identity = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail '需要管理员权限：右键「以管理员身份运行」PowerShell 后重试'
}
if (-not $InstallDir) { $InstallDir = Join-Path $env:ProgramData 'SyncOJ\judge' }
# 清理任务经 cmd.exe 判断标记在不在：路径里的 % 会被 cmd 展开
if ($InstallDir.Contains('%')) { Fail "安装目录不能含 %：$InstallDir" }

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
if (-not (Test-LocalServiceCanRead $PythonExe)) {
    Warn "LOCAL SERVICE 可能读不到 $PythonExe（装在某个用户的目录下？）：评测机会起不来。重装 Python 时选「为所有用户安装」（Install for all users），或用 -Python 指定那份"
}

# ---- 编译器（MinGW g++）----
$gpp = Get-Command g++ -ErrorAction SilentlyContinue
if (-not $gpp) {
    Warn '没有 g++（MinGW-w64）：评测会全部记为系统错误。装好后把它的 bin 目录加进「系统」PATH'
} else {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $gppDir = Split-Path -Parent $gpp.Source
    if (-not (($machinePath -split ';') | Where-Object { $_.TrimEnd('\') -ieq $gppDir.TrimEnd('\') })) {
        Warn "g++ 在 $gppDir，但它不在「系统」PATH 里；计划任务以 LOCAL SERVICE 运行，找不到它"
    } elseif (-not (Test-LocalServiceCanRead $gpp.Source)) {
        Warn "LOCAL SERVICE 可能读不到 $($gpp.Source)：把 MinGW-w64 装到所有用户可读的位置（如 C:\mingw64）"
    } else {
        Say "g++：$($gpp.Source)"
    }
}

# ---- 目录与权限 ----
# 安装目录：不继承上级（ProgramData 允许普通用户建文件），SYSTEM 与 Administrators 完全控制，
# LOCAL SERVICE 只读；它建的子目录归它（CREATOR OWNER），好给工作目录设完整性标签
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
& icacls.exe $InstallDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' `
    '*S-1-3-0:(OI)(CI)(IO)F' "*${LocalServiceSid}:(OI)(CI)RX" | Out-Null
if ($LASTEXITCODE -ne 0) { Warn '安装目录权限没有设上（icacls 失败）' }
$CacheDir = Join-Path $InstallDir 'cache'
$BoxDir = Join-Path $InstallDir 'boxes'
$LogFile = Join-Path $InstallDir 'judge.log'
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $BoxDir | Out-Null
if (-not (Test-Path $LogFile)) { New-Item -ItemType File -Path $LogFile | Out-Null }
foreach ($dir in @($CacheDir, $BoxDir)) {
    & icacls.exe $dir /grant:r "*${LocalServiceSid}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { Warn "没能授权 LOCAL SERVICE 修改 $dir（icacls 失败）" }
}
& icacls.exe $LogFile /grant:r "*${LocalServiceSid}:M" | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "没能授权 LOCAL SERVICE 写日志 $LogFile（icacls 失败）" }

# ---- 程序 ----
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
    if ($actual -ne $PyzSha256) {
        Remove-Item -Force $Tmp
        Fail "评测机程序的 sha256 与脚本里记的不同（$actual）：下载被篡改，或服务端刚升级过；重新复制安装命令再跑"
    }
}
& $PythonExe $Tmp --help | Out-Null
if ($LASTEXITCODE -ne 0) { Remove-Item -Force $Tmp; Fail '下载的评测机程序跑不起来' }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}
Move-Item -Force -Path $Tmp -Destination $Pyz

# ---- 卸载脚本与安装清单 ----
# 卸载脚本由服务端发脚本时嵌进下面的 here-string；从仓库里直接跑时占位符原样留着，取旁边那份
$UninstallScript = @'
__SYNCOJ_UNINSTALL_PS1__
'@
if ($UninstallScript.Trim() -eq ('__SYNCOJ_' + 'UNINSTALL_PS1__')) {
    $localUninstaller = Join-Path $PSScriptRoot 'uninstall.ps1'
    if (-not (Test-Path -LiteralPath $localUninstaller)) { Fail '缺少卸载脚本（从服务端下载 install.ps1 再跑）' }
    $UninstallScript = [IO.File]::ReadAllText($localUninstaller)
}
$UninstallPs1 = Join-Path $InstallDir 'uninstall.ps1'
# UTF-8 BOM：PowerShell 5.1 没有 BOM 时按 ANSI 读
[IO.File]::WriteAllText($UninstallPs1, $UninstallScript.TrimStart([char]0xFEFF), (New-Object Text.UTF8Encoding $true))
# 只有 SYSTEM 与 Administrators 能改（它以 SYSTEM 执行）；LOCAL SERVICE 连读都不需要
& icacls.exe $UninstallPs1 /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "卸载脚本权限没有设上（icacls 失败）：$UninstallPs1" }
$Marker = Join-Path $CacheDir '.uninstall-requested'
# 重装：上一次远程卸载没做完留下的标记会让清理任务把新装的也删掉，清掉
if (Test-Path -LiteralPath $Marker) { Remove-Item -LiteralPath $Marker -Force }
$Manifest = Join-Path $InstallDir 'install.json'
$manifestJson = [ordered]@{
    kind = 'schtask'; service = $TaskName; marker = $Marker
    paths = @($InstallDir); tasks = @($TaskName, $CleanupTaskName); accounts = @()
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText($Manifest, $manifestJson, (New-Object Text.UTF8Encoding $false))

# ---- 令牌：SYSTEM、Administrators 完全控制，LOCAL SERVICE 只读（按 SID 授权，不依赖系统语言）----
$TokenFile = Join-Path $InstallDir 'token'
Set-Content -Path $TokenFile -Value $Token -Encoding Ascii
& icacls.exe $TokenFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' "*${LocalServiceSid}:R" | Out-Null
if ($LASTEXITCODE -ne 0) { Warn '令牌文件权限没有收紧（icacls 失败）' }

# ---- 令牌与数据包缓存：低完整性进程（选手程序）不可读（NO_READ_UP 标签；icacls 设不了，借评测机程序里的函数）----
$protect = 'import sys; sys.path.insert(0, sys.argv[1]); from syncoj_server.judging.sandbox_win import ensure_no_read_up as f; ' +
    'bad = [e for e in (f(p) for p in sys.argv[2:]) if e]; print(*bad, sep=chr(10)); sys.exit(1 if bad else 0)'
$problems = @(& $PythonExe -c $protect $Pyz $TokenFile $CacheDir)
if ($LASTEXITCODE -ne 0) { Warn "没能禁止低完整性进程读令牌与缓存：$($problems -join ' ')" }

# ---- 计划任务 ----
$arguments = @(
    "`"$Pyz`"",
    '--server', $Server,
    '--token-file', "`"$TokenFile`"",
    '--backend', $Backend,
    '--slots', $Slots,
    '--cache-dir', "`"$CacheDir`"",
    '--box-dir', "`"$BoxDir`"",
    '--log-file', "`"$LogFile`"",
    '--install-manifest', "`"$Manifest`""
) -join ' '
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $arguments -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $LocalService -LogonType ServiceAccount -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'SyncOJ judge worker' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# ---- 清理任务（SYSTEM）：评测机以 LOCAL SERVICE 运行，删不了计划任务与安装目录 ----
# 平常只是 cmd.exe 判断一下文件在不在（不起 PowerShell，评测机 CPU 不受打扰）；在了才执行 uninstall.ps1
$cleanupArgs = "/d /c if exist `"$Marker`" %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe " +
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$UninstallPs1`" -IfRequested"
$cleanupAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument $cleanupArgs
# 每天 0 点起、每 2 分钟一次、持续 1 天 = 一直每 2 分钟（不写「无限期」：各版本 Windows 对它的写法不一）
$everyTwoMinutes = New-ScheduledTaskTrigger -Daily -At '00:00'
$everyTwoMinutes.Repetition = (New-ScheduledTaskTrigger -Once -At '00:00' -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$cleanupTriggers = @((New-ScheduledTaskTrigger -AtStartup), $everyTwoMinutes)
$cleanupSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$System = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18').Translate([Security.Principal.NTAccount]).Value
$cleanupPrincipal = New-ScheduledTaskPrincipal -UserId $System -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $CleanupTaskName -Action $cleanupAction -Trigger $cleanupTriggers `
    -Settings $cleanupSettings -Principal $cleanupPrincipal -Description 'SyncOJ judge remote uninstall' -Force | Out-Null

Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ("$state" -eq 'Running') {
    Say "评测机已启动：后端 $Backend，并发 $Slots，服务端 $Server"
    Say "日志：$LogFile"
    Say "卸载：以管理员身份运行 $UninstallPs1"
} else {
    Warn "计划任务状态：$state"
    if (Test-Path $LogFile) { Get-Content -Tail 20 $LogFile }
    Fail '评测机没有起来，见上面的日志'
}
