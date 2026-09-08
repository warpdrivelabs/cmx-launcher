#!/usr/bin/env bash
# CMX 服务管理台启停脚本（Linux / macOS）
# 用法：./run.sh          后台启动，日志写入 logs/launcher.log
#       ./run.sh fg       前台运行（Ctrl+C 停止）
#       ./run.sh stop     停止运行中的实例
#       ./run.sh restart  重启
# 首次运行自动创建 .venv；每次启动幂等补装依赖（快速，已装则跳过）
cd "$(dirname "$0")" || exit 1
PORT=8100
LOG_FILE="logs/launcher.log"
mkdir -p logs

stop_launcher() {
  local pids
  pids=$(lsof -t -i ":$PORT" 2>/dev/null)
  if [ -z "$pids" ]; then
    echo "[cmx-launcher] 未发现运行中的实例（端口 $PORT 无监听）"
    return 0
  fi
  echo "[cmx-launcher] 停止 launcher (pid: $(echo $pids | tr '\n' ' '))..."
  kill $pids 2>/dev/null
  for _ in $(seq 1 15); do                # 最多等 ~4.5s 优雅退出
    lsof -t -i ":$PORT" >/dev/null 2>&1 || { echo "[cmx-launcher] 已停止"; return 0; }
    sleep 0.3
  done
  kill -9 $pids 2>/dev/null
  echo "[cmx-launcher] 已强制停止"
}

ensure_venv() {
  if [ ! -x .venv/bin/python ]; then
    echo "[cmx-launcher] 初始化虚拟环境..."
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q -r requirements.txt
}

case "$1" in
  stop) stop_launcher; exit 0 ;;
esac
ensure_venv
if [ "$1" = "restart" ]; then stop_launcher; fi

# 单实例保护：已在监听则不重复启动
if lsof -t -i ":$PORT" >/dev/null 2>&1; then
  echo "[cmx-launcher] 已有实例在运行（端口 $PORT），如需重启请执行 ./run.sh restart"
  exit 0
fi

# 日志 >10MB 轮转，保留一份旧文件（Linux/macOS stat 参数不同，依次回退）
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)" -gt 10485760 ]; then
  mv "$LOG_FILE" "$LOG_FILE.1"
fi

if [ "$1" = "fg" ]; then
  echo "[cmx-launcher] 前台运行 · http://127.0.0.1:$PORT （Ctrl+C 停止）"
  exec .venv/bin/python -u server.py
fi

nohup .venv/bin/python -u server.py >> "$LOG_FILE" 2>&1 &
pid=$!
sleep 1
if lsof -t -i ":$PORT" >/dev/null 2>&1; then
  echo "[cmx-launcher] 已后台启动 (pid $pid) · http://127.0.0.1:$PORT"
else
  echo "[cmx-launcher] 进程已拉起 (pid $pid)，服务就绪中 · http://127.0.0.1:$PORT"
fi
echo "[cmx-launcher] 日志: $LOG_FILE （实时查看: tail -f $LOG_FILE）"
