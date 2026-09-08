@echo off
rem CMX 服务管理台启停脚本（Windows）
rem 用法：run.bat          后台启动，日志写入 logs\launcher.log
rem       run.bat fg       前台运行（Ctrl+C 停止）
rem       run.bat stop     停止运行中的实例
rem       run.bat restart  重启
rem 首次运行自动创建 .venv；每次启动幂等补装依赖（快速，已装则跳过）
cd /d "%~dp0"
set PORT=8100
if not exist logs mkdir logs
if /i "%~1"=="stop" goto stop

if not exist .venv\Scripts\python.exe (
  echo [cmx-launcher] 初始化虚拟环境...
  python -m venv .venv
)
.venv\Scripts\pip install -q -r requirements.txt
if /i "%~1"=="restart" call :stop
if /i "%~1"=="fg" goto fg

rem 单实例保护：已在监听则不重复启动
netstat -ano -p tcp | findstr /r ":%PORT% .*LISTENING" >nul 2>&1 && (
  echo [cmx-launcher] 已有实例在运行（端口 %PORT%），如需重启请执行 run.bat restart
  exit /b 0
)

rem 后台隐藏启动（无窗口），日志落盘；Start-Process 不支持 stdout/err 同文件，分两个文件
powershell -NoProfile -Command "$p = Start-Process -WindowStyle Hidden -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-u','server.py' -WorkingDirectory '%~dp0' -RedirectStandardOutput '%~dp0logs\launcher.log' -RedirectStandardError '%~dp0logs\launcher.err.log' -PassThru; Write-Host ('[cmx-launcher] 已后台启动 (pid ' + $p.Id + ') · http://127.0.0.1:' + $env:PORT)"
echo [cmx-launcher] 日志: logs\launcher.log
exit /b 0

:fg
echo [cmx-launcher] 前台运行 · http://127.0.0.1:%PORT% （Ctrl+C 停止）
.venv\Scripts\python -u server.py
exit /b 0

:stop
set found=0
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /r ":%PORT% .*LISTENING"') do (
  echo [cmx-launcher] 停止 launcher (pid: %%p)
  taskkill /PID %%p /T /F >nul 2>&1
  set found=1
)
if "%found%"=="0" echo [cmx-launcher] 未发现运行中的实例（端口 %PORT% 无监听）
exit /b 0
