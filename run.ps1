# CMX 服务管理台启停脚本（PowerShell · Windows / Linux / macOS）
# 用法：.\run.ps1           后台启动，日志写入 logs/launcher.log
#       .\run.ps1 fg        前台运行（Ctrl+C 停止）
#       .\run.ps1 stop      停止运行中的实例
#       .\run.ps1 restart   重启
# 可选 .\run.ps1 -Port 8100 覆盖端口（默认 8100）
# 首次运行自动创建 .venv；每次启动幂等补装依赖（快速，已装则跳过）
# 行为对齐 run.sh / run.bat：单实例保护、>10MB 日志轮转保留一份旧文件、Windows 后台隐藏窗口
# 若提示执行策略受限：powershell -ExecutionPolicy Bypass -File .\run.ps1

param(
    [Parameter(Position = 0)]
    [ValidateSet('', 'fg', 'stop', 'restart')]
    [string]$Command = '',

    [int]$Port = 8100
)

$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot

# Windows PowerShell 5.1 没有 $IsWindows 自动变量，统一在此判定平台
$IsWin = if (Test-Path Variable:IsWindows) { $IsWindows } else { $true }
$VenvPython = if ($IsWin) { Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
              else        { Join-Path $PSScriptRoot '.venv/bin/python' }
$LogFile = Join-Path $PSScriptRoot 'logs/launcher.log'
$ErrFile = Join-Path $PSScriptRoot 'logs/launcher.err.log'
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

# ---- 取监听 $Port 的进程 PID（空 = 无实例）。Windows 走 NetTCPIP，回退 netstat；Unix 走 lsof ----
function Get-ListenerPid {
    param([int]$Port)
    if ($IsWin) {
        $conns = $null
        try { $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue } catch { }
        if ($conns) { return @($conns | Select-Object -ExpandProperty OwningProcess -Unique) }
        $rows = @(netstat -ano -p tcp | Select-String ":$Port\s.*LISTENING")
        return @($rows | ForEach-Object { [int](($_.Line.Trim() -split '\s+')[-1]) } | Sort-Object -Unique)
    }
    try { return @(& lsof -t -i ":$Port" 2>$null) } catch { return @() }
}

# ---- 停止运行中的实例（Windows 连进程树一起结束；Unix 先优雅后强制，最多等 ~4.5s） ----
function Stop-Launcher {
    param([int]$Port)
    $listenerPids = @(Get-ListenerPid -Port $Port)
    if ($listenerPids.Count -eq 0) {
        Write-Host "[cmx-launcher] 未发现运行中的实例（端口 $Port 无监听）"
        return
    }
    Write-Host "[cmx-launcher] 停止 launcher (pid: $($listenerPids -join ' '))..."
    if ($IsWin) {
        foreach ($procId in $listenerPids) { taskkill /PID $procId /T /F 2>&1 | Out-Null }
    } else {
        $listenerPids | ForEach-Object { Stop-Process -Id $_ -ErrorAction SilentlyContinue }   # SIGTERM
        for ($i = 0; $i -lt 15; $i++) {
            if (@(Get-ListenerPid -Port $Port).Count -eq 0) { Write-Host '[cmx-launcher] 已停止'; return }
            Start-Sleep -Milliseconds 300
        }
        $listenerPids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }   # SIGKILL
        Write-Host '[cmx-launcher] 已强制停止'
        return
    }
    if (@(Get-ListenerPid -Port $Port).Count -eq 0) { Write-Host '[cmx-launcher] 已停止' }
    else { Write-Host "[cmx-launcher] 警告：端口 $Port 仍有监听，请手动检查" }
}

# ---- 首次运行创建 .venv；每次启动幂等补装依赖 ----
function Initialize-Venv {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host '[cmx-launcher] 初始化虚拟环境...'
        $python = if ($IsWin) { 'python' } else { 'python3' }
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "[cmx-launcher] 创建 .venv 失败，请确认已安装 $python" }
    }
    & $VenvPython -m pip install -q -r requirements.txt
}

# ---------------- 主流程 ----------------
if ($Command -eq 'stop') { Stop-Launcher -Port $Port; exit 0 }

Initialize-Venv
if ($Command -eq 'restart') { Stop-Launcher -Port $Port }

# 单实例保护：已在监听则不重复启动
if (@(Get-ListenerPid -Port $Port).Count -gt 0) {
    Write-Host "[cmx-launcher] 已有实例在运行（端口 $Port），如需重启请执行 .\run.ps1 restart"
    exit 0
}

# 日志 >10MB 轮转，保留一份旧文件
if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt 10MB) {
    Move-Item -LiteralPath $LogFile -Destination "$LogFile.1" -Force
}

if ($Command -eq 'fg') {
    Write-Host "[cmx-launcher] 前台运行 · http://127.0.0.1:$Port （Ctrl+C 停止）"
    & $VenvPython -u server.py
    exit $LASTEXITCODE
}

if ($IsWin) {
    # 后台隐藏启动（无窗口），日志落盘；Start-Process 不支持 stdout/err 同文件，分两个文件（对齐 run.bat）
    $proc = Start-Process -FilePath $VenvPython -ArgumentList '-u', 'server.py' `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile -PassThru
} else {
    # Unix：经 sh 追加写单文件日志（对齐 run.sh）
    $shCmd = "exec '$VenvPython' -u server.py >> '$LogFile' 2>&1"
    $proc = Start-Process -FilePath 'sh' -ArgumentList '-c', $shCmd -WorkingDirectory $PSScriptRoot -PassThru
}

Start-Sleep -Seconds 1
if (@(Get-ListenerPid -Port $Port).Count -gt 0) {
    Write-Host "[cmx-launcher] 已后台启动 (pid $($proc.Id)) · http://127.0.0.1:$Port"
} else {
    Write-Host "[cmx-launcher] 进程已拉起 (pid $($proc.Id))，服务就绪中 · http://127.0.0.1:$Port"
}
if ($IsWin) {
    Write-Host '[cmx-launcher] 日志: logs\launcher.log / logs\launcher.err.log（实时查看: Get-Content logs\launcher.log -Wait）'
} else {
    Write-Host "[cmx-launcher] 日志: logs/launcher.log （实时查看: tail -f logs/launcher.log）"
}
