# CMX Launcher · 开发服务控制台

本地 Web 管理台，用于一键管理 CMX 工作区内的全部开发服务。零写死清单——启动时自动扫描工作区发现服务。

## 功能

- **服务自动发现**：扫描工作区各 Rust 仓（`*-server` bin）与前端 Vite 应用（`package.json` dev 脚本），无需维护服务清单
- **启停管理**：单个服务的 启动 / 停止 / 重启，支持勾选批量操作
- **配置可视化**：下拉选择 `*.toml` 配置文件（启动时注入 `CONFIG_FILE`，不改 `.env`）、ARGS 附加参数、`.env` 在线编辑
- **外部进程接管**：识别"外部运行"（非本工具启动、端口被占）的实例，可按端口终止并接管；归属其他用户时自动弹出 sudo 提权框（密码仅 stdin 传递，不保存不上报）
- **TARGET 磁盘治理**：各仓 `target/` 占用扫描（10 分钟缓存 + 服务启动成功即扫）、一键 / 批量 `cargo clean`，输出实时进日志抽屉
- **实时日志**：SSE 推送、ANSI 彩色还原、自动滚动跟随（上翻暂停 + 未读计数悬浮按钮）、多 tab 日志抽屉
- **系统指标**：整机 CPU / 内存 / 磁盘（5s 刷新），每个服务的进程树 CPU / 内存占用
- **跨平台**：Windows / macOS / Linux

## 快速开始

```bash
./run.sh          # 后台启动，日志写入 logs/launcher.log
./run.sh fg       # 前台运行（Ctrl+C 停止）
./run.sh stop     # 停止运行中的实例
./run.sh restart  # 重启
```

Windows 使用 `run.bat`，或 PowerShell 版 `.\run.ps1`（用法相同；后台日志分 `logs\launcher.log` / `launcher.err.log` 两个文件。若执行策略受限：`powershell -ExecutionPolicy Bypass -File .\run.ps1`）。启动后访问 <http://127.0.0.1:8100>。

默认后台运行、不占终端：日志追加写入 `logs/launcher.log`（超过 10MB 自动轮转保留一份 `.log.1`），实时查看执行 `tail -f logs/launcher.log`；需要前台观察输出时用 `./run.sh fg`。脚本内置单实例保护，重复执行不会拉起第二个实例。

首次运行自动创建 `.venv` 并安装依赖（`fastapi + uvicorn + psutil`，见 `requirements.txt`；未装 psutil 时系统/进程指标自动降级）。

## 页面说明

| 区域 | 说明 |
|------|------|
| 顶栏 | 时钟、CPU / 内存 / 磁盘仪表、工作区路径 |
| 服务区 | 后端服务与前端应用分组展示；每个服务卡片含 状态灯 / 配置选择 / 启停按钮 / CPU·内存 / 日志入口 |
| 状态含义 | 待命 · 启动中 · 运行中 · 外部运行（端口被非本工具进程占用）· 已退出 |
| 磁盘区 | 各仓 `target/` 占用排行，勾选后批量清理（运行中的服务需先停止） |
| 日志抽屉 | 底部多 tab 实时日志，支持彩色输出、自动滚动、未读计数、全部关闭 |

## 常见问题

- **前端服务端口不对 / 状态一直"启动中"**：Vite 端口被占会自动跳号，工具从启动日志解析实际端口，解析失败时兜底探测进程树监听端口，稍等数秒即可。
- **停止外部进程弹 sudo 密码框**：目标进程归属其他用户（如 IDE/终端以不同用户启动），输入密码仅用于本次 `sudo kill`。
- **清理 target 后首次构建变慢**：`cargo clean` 删除全部编译产物，属预期行为。

## 目录结构

```
cmx-launcher/
├── server.py          # FastAPI 后端：服务发现 / 进程管理 / 磁盘扫描 / SSE 日志
├── web/index.html     # 前端单页（无构建步骤，原生 JS）
├── run.sh / run.bat / run.ps1   # 启停脚本（默认后台运行；支持 fg / stop / restart 参数，日志写 logs/）
├── logs/              # 运行日志（launcher.log，>10MB 自动轮转）
└── requirements.txt   # Python 依赖
```
