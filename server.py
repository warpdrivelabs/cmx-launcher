#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CMX 服务启动器 · 本地 Web 管理台
================================
功能：
  - 自动扫描工作区各 Rust 仓，发现 *-server 服务（无需写死清单）
  - 可视化 启动/停止/重启/批量操作；动态生成 `cargo run -p <bin>` 命令（不依赖 *.sh）
  - 配置文件自动扫描（*.toml），启动时注入 CONFIG_FILE（不改 .env）
  - .env 可视化编辑
  - target 目录磁盘占用扫描与一键清理
  - 服务日志实时查看（SSE）

跨平台：Windows / macOS / Linux（Python 3.10+，依赖 fastapi + uvicorn）
启动：python3 server.py  →  http://127.0.0.1:8100
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import psutil
except ImportError:          # 未安装时系统/进程指标功能自动降级
    psutil = None

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# ---------------------------------------------------------------- 路径与常量
BASE_DIR = Path(__file__).resolve().parent          # cmx-launcher/
WORKSPACE = BASE_DIR.parent                          # 工作区根目录
WEB_DIR = BASE_DIR / "web"
STATE_FILE = BASE_DIR / "launcher_state.json"
LOG_KEEP = 5000                                      # 每个服务内存日志保留行数
SKIP_DIRS = {"cmx-launcher", "node_modules", "target", "docs", "documents", "packages", "crates", "dist"}

IS_WIN = sys.platform == "win32"
RE_BIN_NAME = re.compile(r'^name\s*=\s*"([^"]+)"', re.M)
RE_PORT = re.compile(r'^port\s*=\s*(\d+)\s*$', re.M)

app = FastAPI(title="CMX Launcher")

# ---------------------------------------------------------------- 服务自动发现
# bin 名 → 友好显示名（仅用于界面展示；未命中回退为 bin 名，发现逻辑本身零写死）
_NAME_ALIAS = {
    "cmx-portal-server": "门户 Portal",
    "cmx-model-server": "模型 Model",
    "cmx-flow-server": "流程 Flow",
    "cmx-mdm-server": "主数据 MDM",
    "cmx-rpt-server": "报表 Report",
    "cmx-rule-server": "规则 Rules",
}
# 前端应用（package.json name）→ 显示名；默认端口统一 5173（vite 被占自动跳号，实际端口从日志解析）
_NODE_ALIAS = {
    "cmx-portal-manager": "门户管理 PortalMgr",
    "cmx-html-designer": "设计器 Designer"
}

_registry: dict[str, dict] = {}     # sid(仓目录名) -> {dir(相对工作区根), bin, name, kind, script, port}
_reg_lock = threading.Lock()


def _eligible_dir(p: Path) -> bool:
    return p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_DIRS


def _iter_repo_dirs() -> list[Path]:
    """枚举工作区内全部候选仓目录（绝对路径），逐层下钻、深度 ≤3，兼容三种布局：
    根平铺 <root>/<repo>、分组两层 <root>/backend|frontend/<repo>、npm workspace 三层
    <root>/frontend/<ws>/<pkg-app>。是否真是仓由调用方条件判定（Cargo.toml / package.json / target）。"""
    frontier = [d for d in sorted(WORKSPACE.iterdir()) if _eligible_dir(d)]
    found: list[Path] = []
    for _ in range(3):
        nxt: list[Path] = []
        for d in frontier:
            found.append(d)
            nxt.extend(x for x in sorted(d.iterdir()) if _eligible_dir(x))
        frontier = nxt
    return found


def _crates_of(repo: Path) -> list[Path]:
    """仓内候选 crate：crates/* 子目录 + 仓根本身。"""
    crates = list((repo / "crates").glob("*")) if (repo / "crates").is_dir() else []
    return [c for c in crates if (c / "Cargo.toml").is_file()] + [repo]


def discover_services() -> dict[str, dict]:
    """扫描工作区（经 _iter_repo_dirs，兼容分组两层 / npm workspace 三层布局）：
    - Rust 服务：crate 名以 -server 结尾且含 src/main.rs → `cargo run -p <bin>`
    - 前端应用：package.json 有 dev 脚本引用 vite 且存在 vite.config.js → `npm run dev`
    sid 取仓目录名（仓名全局唯一）；dir 为相对工作区根的 posix 路径。
    """
    found: dict[str, dict] = {}
    for repo in _iter_repo_dirs():
        if repo.name in found:
            print(f"[discover] 忽略重复服务目录: {repo.relative_to(WORKSPACE).as_posix()}")
            continue
        rel = repo.relative_to(WORKSPACE).as_posix()
        # --- Rust 服务 ---
        if (repo / "Cargo.toml").is_file():
            for crate in _crates_of(repo):
                text = (crate / "Cargo.toml").read_text("utf-8", errors="replace")
                m = RE_BIN_NAME.search(text)
                if not m:
                    continue
                bin_name = m.group(1)
                if bin_name.endswith("-server") and (crate / "src" / "main.rs").is_file():
                    found[repo.name] = {
                        "dir": rel, "bin": bin_name, "kind": "rust",
                        "name": _NAME_ALIAS.get(bin_name, bin_name),
                        "script": None, "port": None,
                    }
                    break   # 每仓取第一个 server bin
            continue
        # --- 前端 Vite 应用 ---
        pkg = repo / "package.json"
        if not pkg.is_file() or not (repo / "vite.config.js").is_file():
            continue
        try:
            meta = json.loads(pkg.read_text("utf-8"))
        except Exception:
            continue
        scripts = meta.get("scripts") or {}
        dev = str(scripts.get("dev") or "")
        if "vite" not in dev:
            continue
        name = _NODE_ALIAS.get(meta.get("name", ""), meta.get("name", repo.name))
        found[repo.name] = {
            "dir": rel, "bin": "npm run dev", "kind": "node",
            "name": name, "script": "dev", "port": 5173,
        }
    return found


_disk_repos: list[str] = []         # 磁盘清理目标：所有含 target/ 的仓（相对工作区根的路径）


def reload_registry():
    global _registry, _disk_repos
    reg = discover_services()
    with _reg_lock:
        _registry = reg
    _disk_repos = sorted(p.relative_to(WORKSPACE).as_posix()
                         for p in _iter_repo_dirs() if (p / "target").is_dir())
    return reg


def get_service(sid: str) -> dict:
    """按 sid 取服务注册信息，不存在时 404。"""
    with _reg_lock:
        info = _registry.get(sid)
    if not info:
        raise HTTPException(404, f"未知服务: {sid}")
    return info


# ---------------------------------------------------------------- 持久化状态（界面选择）
_state_lock = threading.Lock()
_state: dict = {"settings": {}}   # sid -> {toml: str|None, args: str}


def _load_state():
    global _state
    if STATE_FILE.exists():
        try:
            _state = {**_state, **json.loads(STATE_FILE.read_text("utf-8"))}
        except Exception:
            pass


def _save_state():
    with _state_lock:
        STATE_FILE.write_text(json.dumps(_state, ensure_ascii=False, indent=2), "utf-8")


_load_state()


def _get_settings(sid: str) -> dict:
    return _state["settings"].setdefault(sid, {"toml": None, "args": ""})


# ---------------------------------------------------------------- .env / toml
def _env_path(sid: str) -> Path:
    return service_dir(sid) / ".env"


def _parse_env(text: str) -> dict:
    """轻量 dotenv 解析：KEY=VALUE，忽略注释与空行；带引号值去包裹引号。"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


_TOML_EXCLUDE = {"Cargo.toml", "rust-toolchain.toml", "Cargo.lock"}


def _toml_files(sid: str) -> list[str]:
    """CFG 下拉候选：仓根目录的 *.toml，排除构建/工具链文件（仅应用配置）。"""
    return sorted(p.name for p in service_dir(sid).glob("*.toml")
                  if p.name not in _TOML_EXCLUDE)


def _read_port(sid: str, toml: Optional[str]) -> Optional[int]:
    """从生效配置读取监听端口：选中 toml > .env 的 CONFIG_FILE > 目录内 *-server*.toml。"""
    d = service_dir(sid)
    candidates: list[Path] = []
    if toml:
        candidates.append(d / toml)
    else:
        env_path = d / ".env"
        if env_path.exists():
            cfg = _parse_env(env_path.read_text("utf-8")).get("CONFIG_FILE", "")
            if cfg:
                candidates.append(d / Path(cfg).name)
    candidates += sorted(d.glob("*-server*.toml"))
    for c in candidates:
        if c.is_file():
            m = RE_PORT.search(c.read_text("utf-8", errors="replace"))
            if m:
                return int(m.group(1))
    return None


# ---------------------------------------------------------------- 进程管理
def _pump(pipe, emit):
    """读子进程输出，按 \\n 和 \\r 分帧（cargo 进度条用 \\r 刷新，分帧才能实时滚动）。"""
    buf = ""
    fd = pipe.fileno()
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for piece in parts:
            if piece:
                emit(piece)
    if buf:
        emit(buf)


def _append_log(holder, text: str):
    """向含 .log/.log_seq 的日志载体追加一行（Proc 与 _CleanProc 共用）。"""
    holder.log_seq += 1
    holder.log.append({"seq": holder.log_seq, "ts": time.time(), "text": text})


def _lines_since(p, since: int) -> list[dict]:
    """取日志载体中 seq 大于 since 的行（logs API 与 SSE 流共用）。"""
    return [l for l in p.log if l["seq"] > since]


class Proc:
    def __init__(self, sid: str, popen: subprocess.Popen, args: str, toml: Optional[str]):
        self.sid = sid
        self.popen = popen
        self.started_at = time.time()
        self.args = args
        self.toml = toml
        self.actual_port: Optional[int] = None     # 前端应用：从启动日志解析的实际端口
        self.exit_code: Optional[int] = None
        self.log: deque = deque(maxlen=LOG_KEEP)
        self.log_seq = 0
        self._reader = threading.Thread(target=self._read_logs, daemon=True)
        self._reader.start()

    def _emit(self, text: str):
        _append_log(self, text)
        # 前端应用：从 vite 启动日志解析实际监听端口（vite 端口被占会自动跳号）。
        # 注意 vite 8 会给端口单独上色（如 http://localhost:\x1b[1m5173\x1b[22m/），
        # 必须先剥离 ANSI 转义序列再匹配，否则端口号永远解析不到。
        if not self.actual_port and _registry.get(self.sid, {}).get("kind") == "node":
            bare = _ANSI_RE.sub("", text)
            m = re.search(r"https?://(?:localhost|127\.0\.0\.1):(\d+)", bare)
            if m:
                self.actual_port = int(m.group(1))
                with _state_lock:
                    _get_settings(self.sid)["port"] = self.actual_port
                _save_state()

    def _read_logs(self):
        _pump(self.popen.stdout, self._emit)
        self.exit_code = self.popen.wait()
        # 启动进程退出（无论编译失败退出还是被停止）→ 重扫该仓 target：
        # 失败场景不经过 running 状态转变，必须在此兜底；重复触发由 _scan_repo 的
        # scanning 互斥去重（如 restart 时 stop 退出已扫、新进程 running 转变不重复扫）。
        threading.Thread(target=_scan_repo, args=(self.sid,), daemon=True).start()

    def alive(self) -> bool:
        return self.popen.poll() is None


_procs: dict[str, Proc] = {}
_proc_lock = threading.Lock()


def service_dir(sid: str) -> Path:
    return WORKSPACE / get_service(sid)["dir"]


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _detect_listen_port(root_pid: int) -> Optional[int]:
    """日志未解析到端口时的兜底：遍历进程树监听中的 socket 找 dev 端口。"""
    if psutil is None:
        return None
    try:
        root = psutil.Process(root_pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    ports = []
    for pr in procs:
        try:
            for c in pr.net_connections(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr:
                    ports.append(c.laddr.port)
        except psutil.Error:
            continue
    dev = [p_ for p_ in ports if 3000 <= p_ <= 9999]
    return min(dev) if dev else None


def _pids_of_port(port: int) -> list[int]:
    """找到监听指定端口的进程 PID（跨平台：lsof → ss → netstat 逐级回退）。"""
    pids: list[int] = []
    try:
        if IS_WIN:
            r = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                               capture_output=True, text=True, timeout=10)
            for ln in r.stdout.splitlines():
                parts = ln.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                    try:
                        pids.append(int(parts[4]))
                    except ValueError:
                        pass
        else:
            try:
                r = subprocess.run(["lsof", "-t", "-i", f":{port}"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    pids = [int(x) for x in r.stdout.split()]
            except FileNotFoundError:
                r = subprocess.run(["ss", "-ltnp", f"sport = :{port}"],
                                   capture_output=True, text=True, timeout=10)
                for ln in r.stdout.splitlines():
                    m = re.search(r"pid=(\d+)", ln)
                    if m:
                        pids.append(int(m.group(1)))
    except Exception:
        pass
    return sorted({p for p in pids if p != os.getpid()})   # 永远排除本工具自己


def _kill_pids(pids: list[int], force: bool = False) -> tuple[list[int], bool]:
    """返回 (成功终止的 pid, 是否遇到权限不足)。"""
    killed, perm_denied = [], False
    for pid in pids:
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            perm_denied = True
        except SubprocessError:
            continue
    return killed, perm_denied


def _sudo_kill(pids: list[int], password: str, force: bool = False) -> tuple[bool, str]:
    """通过 sudo -S 从 stdin 验证密码后终止进程。返回 (是否成功, 错误信息)。"""
    sig = "-9" if force else "-15"
    try:
        r = subprocess.run(
            ["sudo", "-S", "-p", "", "kill", sig] + [str(p) for p in pids],
            input=password + "\n", capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return False, "系统未安装 sudo"
    except subprocess.TimeoutExpired:
        return False, "sudo 执行超时"
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        low = err.lower()
        if "incorrect password" in low or "no password was provided" in low or "a password is required" in low:
            return False, "sudo 密码验证失败"
        return False, err or f"sudo 退出码 {r.returncode}"
    return True, ""


def _log(sid: str, text: str):
    """工具自身的运维日志，写入与子进程相同的 buffer。"""
    p = _procs.get(sid)
    if p is None:
        return
    _append_log(p, text)


def start_service(sid: str, toml: Optional[str] = None, args: str = "") -> dict:
    info = get_service(sid)
    with _proc_lock:
        old = _procs.get(sid)
        if old and old.alive():
            raise HTTPException(409, f"{info['name']} 已在运行 (pid={old.popen.pid})")
    # 端口被外部实例占用 → 拒绝，避免拉起必然绑定失败的重复进程
    st_now = status_of(sid)
    if st_now["status"] == "external":
        raise HTTPException(409, f"{info['name']} 端口 {st_now['port']} 已被占用（疑似外部启动的实例）")

    d = service_dir(sid)
    env = dict(os.environ)
    # 环境：os.environ + .env（不覆盖已有，语义同 dotenvy） + 启动时注入
    env_path = _env_path(sid)
    if env_path.exists():
        for k, v in _parse_env(env_path.read_text("utf-8")).items():
            env.setdefault(k, v)
    inject_note = ""

    if info["kind"] == "node":
        # 前端应用：npm run dev [-- args]，不锁端口（vite 端口被占自动跳号，实际端口从启动日志解析）
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise HTTPException(400, "未找到 npm，请确认 Node.js 在 PATH 中")
        cmd = [npm, "run", info["script"]]
        if args.strip():
            cmd += ["--"] + args.split()
        env["FORCE_COLOR"] = "1"   # vite/chalk 在管道下也输出 ANSI 颜色
    else:
        # Rust 服务：动态生成 cargo run -p <bin> [args]，cwd=服务仓（dotenvy 自动读 .env）
        cargo = shutil.which("cargo")
        if not cargo:
            raise HTTPException(400, "未找到 cargo，请确认 Rust 工具链在 PATH 中")
        cmd = [cargo, "run", "-p", info["bin"]] + (args.split() if args.strip() else [])
        if toml:
            env["CONFIG_FILE"] = f"./{toml}"
            inject_note = f"（注入 CONFIG_FILE=./{toml}）"
        # 强制 cargo/rustc 输出 ANSI 颜色（管道捕获非 TTY 时默认关闭）
        env["CARGO_TERM_COLOR"] = "always"

    popen = subprocess.Popen(
        cmd, cwd=str(d), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=not IS_WIN,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0,
    )
    p = Proc(sid, popen, args, toml if info["kind"] == "rust" else None)
    with _proc_lock:
        _procs[sid] = p
    _log(sid, f"[launcher] {' '.join(cmd)}  {inject_note}pid={popen.pid}")
    return {"ok": True, "pid": popen.pid, "injected": toml}


def stop_service(sid: str, sudo_pw: Optional[str] = None) -> dict:
    info = get_service(sid)
    with _proc_lock:
        p = _procs.get(sid)
    if p is not None and p.alive():
        pid = p.popen.pid
        _log(sid, f"[launcher] 停止 {info['name']} pid={pid}")
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + 10
        while p.alive() and time.time() < deadline:
            time.sleep(0.2)
        if p.alive() and not IS_WIN:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return {"ok": True, "stopped": f"{info['name']} (pid={pid})"}
    # 非本工具启动 → 按端口找到监听进程并终止（外部运行接管）
    st = status_of(sid)
    if st["port"] and port_listening(st["port"]):
        port = st["port"]
        pids = _pids_of_port(port)
        if not pids:
            raise HTTPException(409, f"{info['name']} 端口 {port} 在监听但未定位到进程 PID")
        killed, perm_denied = _kill_pids(pids)
        if perm_denied:
            # 存在归属其他用户的进程（含混合归属：本用户可杀的已杀掉，剩余必须 sudo）
            if not sudo_pw:
                raise HTTPException(409, {
                    "message": f"{info['name']} 外部进程 (pid={','.join(map(str, pids))}) 归属其他用户，需要提权（sudo）终止",
                    "need_auth": True})
            ok, err = _sudo_kill(_pids_of_port(port) or pids, sudo_pw)
            if not ok:
                raise HTTPException(409, {"message": f"{info['name']} 提权终止失败：{err}",
                                          "need_auth": "密码" not in err})
            killed = pids
        elif not killed:
            raise HTTPException(409, f"{info['name']} 外部进程 (pid={','.join(map(str, pids))}) 终止失败")
        for _ in range(15):                 # 最多 ~4.5s 等端口释放，超时强杀
            if not port_listening(port):
                break
            time.sleep(0.3)
        else:
            if sudo_pw:
                _sudo_kill(_pids_of_port(port) or pids, sudo_pw, force=True)
            else:
                _kill_pids(pids, force=True)
        if port_listening(port):            # 终局校验：端口仍未释放则如实报错，不误报成功
            raise HTTPException(409, f"{info['name']} 端口 {port} 仍被占用，外部进程终止失败（可重试并提权 sudo）")
        return {"ok": True, "stopped": f"{info['name']}（外部进程 pid={','.join(map(str, killed))}）"}
    raise HTTPException(409, f"{info['name']} 未在运行")


def status_of(sid: str) -> dict:
    info = get_service(sid)
    p = _procs.get(sid)
    settings = _get_settings(sid)
    env_cfg = None
    kind = info.get("kind", "rust")
    url = None
    if kind == "node":
        # 前端应用：实际端口从启动日志解析（vite 跳号），历史值兜底
        port = (p.actual_port if p else None) or settings.get("port") or info.get("port")
        # 日志解析失败时兜底：探测进程树实际监听的端口
        if p and p.alive() and p.actual_port is None:
            dp = _detect_listen_port(p.popen.pid)
            if dp:
                p.actual_port = dp
                with _state_lock:
                    _get_settings(sid)["port"] = dp
                _save_state()
                port = dp
        url = f"http://127.0.0.1:{port}" if port else None
        # 外部运行判定只信本服务实际观测过的端口（启动日志解析 / 进程树探测 / 历史记录）：
        # 默认 5173 是所有 vite 应用的公共兜底值，可能被其它前端应用占用，据它判定会张冠李戴
        observed = (p.actual_port if p else None) or settings.get("port")
        listening = port_listening(observed) if observed else False
    else:
        env_path = _env_path(sid)
        if env_path.exists():
            env_cfg = _parse_env(env_path.read_text("utf-8")).get("CONFIG_FILE")
        port = _read_port(sid, settings.get("toml"))
        listening = port_listening(port) if port else False
    item = {
        "sid": sid, "name": info["name"], "bin": info["bin"], "dir": info["dir"],
        "kind": kind, "url": url,
        "port": port, "running": False, "status": "stopped",
        "pid": None, "uptime": 0, "exit_code": None,
        "port_listening": listening,
        "toml": settings.get("toml"), "args": settings.get("args", ""),
        "env_config_file": env_cfg,
    }
    if p and p.alive():
        item.update(running=True, pid=p.popen.pid, uptime=int(time.time() - p.started_at),
                    status="running" if listening else "starting")
    elif listening:
        # 端口被非本工具进程占用 → 外部运行（可停止/重启接管）。判定不被历史 Proc 掩盖：
        # 本工具曾启动过的服务退出后（Proc 仍留 _procs 供日志查看），只要配置端口仍在
        # 监听就说明是外部实例或孤儿子进程，按端口定位 PID 展示并支持接管
        ext_pids = _pids_of_port(port)
        item.update(status="external", running=True, pid=ext_pids[0] if ext_pids else None)
    elif p is not None:
        item["exit_code"] = p.exit_code
    return item


# ---------------------------------------------------------------- 磁盘扫描与清理
_disk_lock = threading.Lock()
_disk_cache: dict[str, dict] = {}      # repo -> {size, exists, scanning, cleaning, scanned_at}


def _dir_size(path: Path) -> int:
    if IS_WIN:
        total = 0
        for root, _dirs, files in os.walk(path, onerror=lambda e: None):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    if r.returncode == 0:
        return int(r.stdout.split()[0]) * 1024
    return 0


def _scan_repo(repo: str):
    target = WORKSPACE / repo / "target"
    with _disk_lock:
        item = _disk_cache.setdefault(repo, {})
        if item.get("scanning") or item.get("cleaning"):
            return
        item["scanning"] = True
    try:
        size = _dir_size(target) if target.exists() else 0
        with _disk_lock:
            item.update(size=size, exists=target.exists(), scanned_at=time.time())
    finally:
        with _disk_lock:
            item["scanning"] = False


def _scan_all_async():
    for repo in _disk_repos:
        threading.Thread(target=_scan_repo, args=(repo,), daemon=True).start()


# cargo clean 伪服务：清理输出实时进日志抽屉（sid = __clean__）
CLEAN_SID = "__clean__"


class _CleanProc:
    """与 Proc 同接口的轻量日志载体（.log/.log_seq/.alive），供 SSE 与 logs API 使用。"""

    def __init__(self):
        self.log: deque = deque(maxlen=LOG_KEEP)
        self.log_seq = 0
        self.exit_code: Optional[int] = None
        self._alive = True

    def emit(self, text: str):
        _append_log(self, text)

    def finish(self):
        self._alive = False

    def alive(self) -> bool:
        return self._alive


_clean_lock = threading.Lock()
_clean_busy = False


def _clean_worker(repos: list[str]):
    global _clean_busy
    p = _CleanProc()
    with _proc_lock:
        _procs[CLEAN_SID] = p
    cargo = shutil.which("cargo")
    if not cargo:
        p.emit("[launcher] 未找到 cargo，无法执行 cargo clean")
        p.finish()
        with _clean_lock:
            _clean_busy = False
        return
    env = dict(os.environ)
    env["CARGO_TERM_COLOR"] = "always"   # cargo clean 摘要也带颜色
    try:
        for repo in repos:
            target = WORKSPACE / repo / "target"
            with _disk_lock:
                item = _disk_cache.setdefault(repo, {})
                if item.get("cleaning"):
                    continue
                item["cleaning"] = True
            try:
                before = item.get("size") or 0
                p.emit(f"[launcher] $ cargo clean · {repo}/target · 当前占用 {before/1024**3:.1f} GB")
                popen = subprocess.Popen(
                    [cargo, "clean"], cwd=str(WORKSPACE / repo), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=not IS_WIN,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0,
                )
                pump = threading.Thread(
                    target=_pump, args=(popen.stdout, lambda t, r=repo: p.emit(f"{r} │ {t}")),
                    daemon=True)
                pump.start()
                rc = popen.wait()
                pump.join(timeout=3)
                p.emit(f"[launcher] {repo} cargo clean 结束，exit {rc}")
                with _disk_lock:
                    item.update(size=0, exists=target.exists(), scanned_at=time.time())
            finally:
                with _disk_lock:
                    item["cleaning"] = False
        p.emit("[launcher] ✅ 全部清理完成")
    finally:
        p.finish()
        with _clean_lock:
            _clean_busy = False


# ---------------------------------------------------------------- API
class StartBody(BaseModel):
    toml: Optional[str] = None
    args: str = ""


class BatchBody(BaseModel):
    sids: list[str]
    sudo_password: Optional[str] = None


class StopBody(BaseModel):
    sudo_password: Optional[str] = None


class EnvBody(BaseModel):
    raw: str


class SettingsBody(BaseModel):
    toml: Optional[str] = None
    args: str = ""


class CleanBody(BaseModel):
    repos: list[str]


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text("utf-8")


@app.get("/monitor", response_class=HTMLResponse)
def monitor():
    return (WEB_DIR / "monitor.html").read_text("utf-8")


# 本地打包的前端资产（uPlot 等；运行时纯本地，不走 CDN）
app.mount("/vendor", StaticFiles(directory=str(WEB_DIR / "vendor")), name="vendor")


@app.post("/api/rescan")
def api_rescan():
    reg = reload_registry()
    return {"ok": True, "services": [v["name"] for v in reg.values()],
            "disk_repos": _disk_repos}


# ---------------------------------------------------------------- CPU / 内存 / IO 指标
_stats: dict = {"cpu": None, "mem_pct": None, "mem_used": None, "mem_total": None,
                "disk_r": None, "disk_w": None, "load": None, "services": {}}

# 监控页时序历史（内存环缓，重启清零）+ IO 差分基线
_HIST_N = 180                                       # 2s × 180 ≈ 6 分钟窗
_hist_lock = threading.Lock()
_hist_sys: deque = deque(maxlen=_HIST_N)            # 每帧 {t,cpu,memPct,memUsed,memTotal,diskR,diskW,load}
_hist_svc: dict[str, deque] = {}                    # sid -> deque(帧 {t,cpu,mem,ioR,ioW})
_io_prev: dict[str, tuple] = {}                     # sid -> (read_bytes累计, write_bytes累计, ts)
_sys_io_prev: Optional[tuple] = None               # (read_bytes, write_bytes, ts)
_io_supported: Optional[bool] = None               # 每进程 IO 计数器本平台是否可用（macOS 不支持）


def _sample_service_stats(sid: str) -> Optional[dict]:
    """统计单个服务进程树（含子进程）的 CPU% / 内存 RSS / 线程 / 句柄 / 磁盘 IO 速率。
    我们启动的取句柄 pid；外部运行的按端口定位 PID。IO 计数器在 macOS 不支持 → io_r/io_w=None。"""
    if psutil is None:
        return None
    p = _procs.get(sid)
    root = p.popen.pid if (p and p.alive()) else None
    if root is None:
        st = status_of(sid)
        if st["status"] == "external" and st["port"]:
            pids = _pids_of_port(st["port"])
            root = pids[0] if pids else None
    if root is None:
        return None
    try:
        procs = [psutil.Process(root), *psutil.Process(root).children(recursive=True)]
    except psutil.Error:
        return None
    cpu, mem, threads, fds = 0.0, 0, 0, 0
    io_r_cum, io_w_cum, io_ok = 0, 0, False
    for x in procs:
        try:
            cpu += x.cpu_percent(interval=None)
            mem += x.memory_info().rss
            threads += x.num_threads()
            try:
                fds += x.num_fds() if hasattr(x, "num_fds") else x.num_handles()
            except (psutil.Error, AttributeError):
                pass
            try:                                    # macOS 无每进程 IO 计数器 → 跳过
                io = x.io_counters()
                io_r_cum += io.read_bytes
                io_w_cum += io.write_bytes
                io_ok = True
            except (psutil.Error, AttributeError, NotImplementedError):
                pass
        except psutil.Error:
            continue
    now = time.time()
    io_r = io_w = None
    if io_ok:
        prev = _io_prev.get(sid)
        _io_prev[sid] = (io_r_cum, io_w_cum, now)
        if prev and now > prev[2]:                  # 差分累计字节 → B/s（首帧无基线跳过）
            dt = now - prev[2]
            io_r = max(0.0, (io_r_cum - prev[0]) / dt)
            io_w = max(0.0, (io_w_cum - prev[1]) / dt)
    return {"cpu": round(cpu, 1), "mem_mb": round(mem / 1048576, 1),
            "threads": threads, "fds": fds,
            "io_r": round(io_r) if io_r is not None else None,
            "io_w": round(io_w) if io_w is not None else None}


def _stats_loop():
    """后台每 2s 采样系统与各服务进程指标 + 追加时序历史，供 API 读缓存（不阻塞请求）。"""
    global _sys_io_prev, _io_supported
    if psutil is None:
        return
    psutil.cpu_percent(interval=None)               # 首次调用预热
    try:                                            # 探测每进程 IO 计数器是否可用（本工具自身进程）
        psutil.Process().io_counters()
        _io_supported = True
    except Exception:
        _io_supported = False
    try:
        dio = psutil.disk_io_counters()
        _sys_io_prev = (dio.read_bytes, dio.write_bytes, time.time()) if dio else None
    except Exception:
        _sys_io_prev = None
    while True:
        try:
            now = time.time()
            for sid in list(_registry):
                s = _sample_service_stats(sid)
                if s:
                    _stats["services"][sid] = s
                    with _hist_lock:
                        dq = _hist_svc.setdefault(sid, deque(maxlen=_HIST_N))
                        dq.append({"t": now, "cpu": s["cpu"], "mem": s["mem_mb"],
                                   "ioR": s["io_r"], "ioW": s["io_w"]})
                else:
                    _stats["services"].pop(sid, None)
            vm = psutil.virtual_memory()
            disk_r = disk_w = None                   # 系统级磁盘 IO 速率（全平台可用）
            try:
                dio = psutil.disk_io_counters()
                if dio and _sys_io_prev and now > _sys_io_prev[2]:
                    dt = now - _sys_io_prev[2]
                    disk_r = max(0.0, (dio.read_bytes - _sys_io_prev[0]) / dt)
                    disk_w = max(0.0, (dio.write_bytes - _sys_io_prev[1]) / dt)
                if dio:
                    _sys_io_prev = (dio.read_bytes, dio.write_bytes, now)
            except Exception:
                pass
            load = None
            try:
                load = round(os.getloadavg()[0], 2)
            except (OSError, AttributeError):        # Windows 无 getloadavg
                pass
            cpu = psutil.cpu_percent(interval=None)
            _stats.update(cpu=cpu, mem_pct=vm.percent, mem_used=vm.used, mem_total=vm.total,
                          disk_r=disk_r and round(disk_r), disk_w=disk_w and round(disk_w), load=load)
            with _hist_lock:
                _hist_sys.append({"t": now, "cpu": cpu, "memPct": vm.percent,
                                  "memUsed": vm.used, "memTotal": vm.total,
                                  "diskR": disk_r and round(disk_r), "diskW": disk_w and round(disk_w),
                                  "load": load})
        except Exception:
            pass
        time.sleep(2)


@app.get("/api/system")
def api_system():
    return _stats


@app.get("/api/metrics")
def api_metrics():
    """监控页数据源：系统 + 各服务的当前值 + 时序历史（列式数组，uPlot 直接吃）。"""
    if psutil is None:
        return {"sampleInterval": 2, "ioSupported": False, "psutil": False,
                "system": {"cur": {}, "series": {"t": []}}, "services": {}}
    if not _registry:
        reload_registry()
    with _hist_lock:
        sys_frames = list(_hist_sys)
        svc_frames = {sid: list(dq) for sid, dq in _hist_svc.items()}

    def col(frames, key):
        return [f.get(key) for f in frames]

    sys_series = {
        "t": [round(f["t"], 1) for f in sys_frames],
        "cpu": col(sys_frames, "cpu"), "memPct": col(sys_frames, "memPct"),
        "diskR": col(sys_frames, "diskR"), "diskW": col(sys_frames, "diskW"),
    }
    services = {}
    for sid, info in dict(_registry).items():
        frames = svc_frames.get(sid, [])
        cur = _stats["services"].get(sid, {})
        p = _procs.get(sid)
        if p and p.alive():                          # 轻量状态：不做端口探测（避免每 2s 阻塞）
            status = "running"
        elif sid in _stats["services"]:
            status = "external"
        else:
            status = "stopped"
        services[sid] = {
            "name": info["name"], "kind": info.get("kind", "rust"),
            "status": status, "pid": (p.popen.pid if (p and p.alive()) else None),
            "cur": cur,
            "series": {
                "t": [round(f["t"], 1) for f in frames],
                "cpu": col(frames, "cpu"), "mem": col(frames, "mem"),
                "ioR": col(frames, "ioR"), "ioW": col(frames, "ioW"),
            },
        }
    return {
        "sampleInterval": 2, "ioSupported": bool(_io_supported), "psutil": True,
        "system": {
            "cur": {"cpu": _stats.get("cpu"), "memPct": _stats.get("mem_pct"),
                    "memUsed": _stats.get("mem_used"), "memTotal": _stats.get("mem_total"),
                    "diskR": _stats.get("disk_r"), "diskW": _stats.get("disk_w"),
                    "load": _stats.get("load")},
            "series": sys_series,
        },
        "services": services,
    }


_prev_status: dict[str, str] = {}   # 上轮状态，用于捕捉 stopped/starting → running 转变


@app.get("/api/services")
def list_services():
    if not _registry:
        reload_registry()
    items = [status_of(sid) for sid in _registry]
    items.sort(key=lambda x: (x["port"] or 9999, x["sid"]))
    # 并入 CPU/内存指标（后台线程 2s 采样缓存）
    for it in items:
        it.update(_stats["services"].get(it["sid"], {}) or {})
    # 服务启动成功（端口开始监听）→ 立即扫描该服务 target 占用
    # （失败退出的场景由 Proc._read_logs 的进程退出钩子兜底，成功+失败都会触发扫描）
    for it in items:
        sid = it["sid"]
        if it["status"] == "running" and _prev_status.get(sid) != "running":
            threading.Thread(target=_scan_repo, args=(it["dir"],), daemon=True).start()
        _prev_status[sid] = it["status"]
    return {"services": items, "workspace": str(WORKSPACE)}


@app.post("/api/services/{sid}/start")
def api_start(sid: str, body: StartBody):
    with _state_lock:
        s = _get_settings(sid)
        s["toml"], s["args"] = body.toml, body.args
    _save_state()
    return start_service(sid, body.toml, body.args)


@app.post("/api/services/{sid}/stop")
def api_stop(sid: str, body: Optional[StopBody] = None):
    return stop_service(sid, body.sudo_password if body else None)


def _stop_if_running(sid: str, sudo_pw: Optional[str] = None):
    """服务运行中（本工具进程或外部进程按端口接管）则先停止，已停止则不动。"""
    if _procs.get(sid) and _procs[sid].alive():
        stop_service(sid)
        time.sleep(0.5)
    elif status_of(sid)["status"] == "external":
        stop_service(sid, sudo_pw)   # 外部进程：按端口终止
        time.sleep(0.5)


@app.post("/api/services/{sid}/restart")
def api_restart(sid: str, body: Optional[StopBody] = None):
    st = status_of(sid)
    _stop_if_running(sid, body.sudo_password if body else None)
    return start_service(sid, st["toml"], st["args"])


@app.post("/api/batch/start")
def api_batch_start(body: BatchBody):
    results = {}
    for i, sid in enumerate(body.sids):
        with _state_lock:
            s = _get_settings(sid)
        try:
            results[sid] = start_service(sid, s.get("toml"), s.get("args", ""))
        except HTTPException as e:
            results[sid] = {"ok": False, "error": e.detail}
        if i < len(body.sids) - 1:
            time.sleep(0.8)   # 错峰启动，避免 cargo 锁排队看不到日志
    return {"results": results}


@app.post("/api/batch/stop")
def api_batch_stop(body: BatchBody):
    results = {}
    for sid in body.sids:
        try:
            results[sid] = stop_service(sid, body.sudo_password)
        except HTTPException as e:
            results[sid] = {"ok": False, "error": e.detail}
    return {"results": results}


@app.post("/api/batch/restart")
def api_batch_restart(body: BatchBody):
    """批量重启：运行中（含外部运行，按端口终止）停止后重启；已停止的直接启动。"""
    results = {}
    for i, sid in enumerate(body.sids):
        try:
            st = status_of(sid)
            _stop_if_running(sid, body.sudo_password)
            with _state_lock:
                s = _get_settings(sid)
            results[sid] = start_service(
                sid, s.get("toml") if st["kind"] == "rust" else None, s.get("args", ""))
        except HTTPException as e:
            results[sid] = {"ok": False, "error": e.detail}
        if i < len(body.sids) - 1:
            time.sleep(0.8)
    return {"results": results}


@app.put("/api/services/{sid}/settings")
def api_settings(sid: str, body: SettingsBody):
    with _state_lock:
        s = _get_settings(sid)
        s["toml"], s["args"] = body.toml, body.args
    _save_state()
    return {"ok": True}


@app.get("/api/services/{sid}/env")
def api_get_env(sid: str):
    p = _env_path(sid)
    if not p.exists():
        return {"raw": "", "exists": False}
    return {"raw": p.read_text("utf-8"), "exists": True}


@app.put("/api/services/{sid}/env")
def api_put_env(sid: str, body: EnvBody):
    get_service(sid)
    if _procs.get(sid) and _procs[sid].alive():
        raise HTTPException(409, "服务运行中，请先停止再修改 .env（避免运行时与配置不一致）")
    _env_path(sid).write_text(body.raw, "utf-8")
    return {"ok": True}


@app.get("/api/services/{sid}/tomls")
def api_tomls(sid: str):
    return {"tomls": _toml_files(sid)}


@app.get("/api/services/{sid}/logs")
def api_logs(sid: str, since: int = 0):
    p = _procs.get(sid)
    if p is None:
        return {"lines": [], "cursor": 0, "running": False}
    lines = _lines_since(p, since)
    return {"lines": lines[-800:], "cursor": p.log_seq, "running": p.alive()}


@app.post("/api/services/{sid}/logs/clear")
def api_logs_clear(sid: str):
    """清空该服务的日志缓冲（Proc 与 _CleanProc 通用）。

    只清内容、保留 log_seq 单调递增：活跃 SSE 流的游标过滤不受影响，
    清除后新日志照常推送到前端；重新打开 tab 时 since=0 拉到的也只有清除后的日志。
    """
    p = _procs.get(sid)
    if p is None:
        raise HTTPException(404, f"服务无日志缓冲: {sid}")
    p.log.clear()
    return {"ok": True}


@app.get("/api/services/{sid}/logs/stream")
def api_log_stream(sid: str, since: int = 0):
    def gen():
        cursor = since
        while True:
            p = _procs.get(sid)
            if p is None:
                # 本工具未启动过该服务：静默关闭流，不提示"进程已结束"
                yield f"data: {json.dumps({'lines': [], 'cursor': 0, 'closed': True, 'silent': True})}\n\n"
                break
            lines = _lines_since(p, cursor)
            if lines:
                cursor = lines[-1]["seq"]
                yield f"data: {json.dumps({'lines': lines, 'cursor': cursor, 'running': p.alive()})}\n\n"
            elif not p.alive():
                yield f"data: {json.dumps({'lines': [], 'cursor': cursor, 'closed': True, 'running': False})}\n\n"
                break
            time.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/disk")
def api_disk():
    if not _disk_repos:
        reload_registry()
    with _disk_lock:
        repos = {r: {**_disk_cache.get(r, {})} for r in _disk_repos}
    now = time.time()
    need_scan = False
    for r in _disk_repos:
        item = repos[r]
        if not item or (now - item.get("scanned_at", 0) > 600
                        and not item.get("scanning") and not item.get("cleaning")):
            need_scan = True
    if need_scan:
        _scan_all_async()
    du = shutil.disk_usage(WORKSPACE)
    return {
        "workspace": str(WORKSPACE),
        "total": du.total, "used": du.used, "free": du.free,
        "percent": round(du.used / du.total * 100, 1),
        "repos": repos,
    }


@app.post("/api/disk/scan")
def api_disk_scan():
    _scan_all_async()
    return {"ok": True}


@app.post("/api/disk/clean")
def api_disk_clean(body: CleanBody):
    global _clean_busy
    for repo in body.repos:
        if repo not in _disk_repos:
            raise HTTPException(400, f"未知仓库: {repo}")
        # 该仓服务运行中时禁止清理（Windows 下产物被锁；其余平台也易出诡异问题）
        info = _registry.get(repo)
        if info and _procs.get(repo) and _procs[repo].alive():
            raise HTTPException(409, f"{info['name']} 运行中，请先停止再清理 {repo}/target")
    with _clean_lock:
        if _clean_busy:
            raise HTTPException(409, "已有清理任务在进行中")
        _clean_busy = True
    threading.Thread(target=_clean_worker, args=(body.repos,), daemon=True).start()
    return {"ok": True, "cleaning": body.repos, "log_sid": CLEAN_SID}


def main():
    import uvicorn
    reg = reload_registry()
    print("CMX Launcher · http://127.0.0.1:8100")
    print(f"workspace: {WORKSPACE}")
    for sid, info in reg.items():
        cmd = "npm run dev" if info["kind"] == "node" else f"cargo run -p {info['bin']}"
        print(f"  - {info['name']:<16} {sid}  ({cmd})")
    print(f"disk repos: {', '.join(_disk_repos)}")
    if psutil is None:
        print("提示: 未安装 psutil，CPU/内存指标功能已降级（pip install psutil 可启用）")
    threading.Thread(target=_stats_loop, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")


if __name__ == "__main__":
    main()
