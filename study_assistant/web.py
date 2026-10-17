"""网页界面：FastAPI 版。

从标准库的 `http.server` 换过来，换的是**能力**不是风格：

  手写 http.server                        FastAPI
  ------------------------------------   ------------------------------------
  路由靠 if/elif 串                    ->  @app.get / @app.post 装饰器
  请求体自己 json.loads + 手写校验      ->  Pydantic 模型，类型不对自动 422
  JSON 序列化、错误响应全靠手写         ->  框架统一处理
  SSE 要自己拼分块、自己管连接          ->  StreamingResponse
  没有接口文档                          ->  /docs 自动生成

单用户本地应用本来用不上这些 —— 但当接口涨到十几个、又要加流式输出时，
自己维护这层就没有收益了。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import AsyncIterator, Literal

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from study_assistant import config, db, repo, seed
from study_assistant import view_model as vm
from study_assistant.llm import ChatClient, LLMError

UI_PATH = Path(__file__).with_name("ui.html")
BASE_PORT = 8765
PORT_TRIES = 12
IDLE_TIMEOUT_SECONDS = 2 * 60 * 60


# --------------------------------------------------------------------------
# 浏览器：怎么把界面"开成一个独立窗口"
# --------------------------------------------------------------------------
def _use_exclusive_port(sock: socket.socket) -> None:
    """让 socket 独占端口。

    Windows 上 SO_REUSEADDR 会允许两个进程绑同一个端口，所以端口探测
    必须用 SO_EXCLUSIVEADDRUSE，否则会"探测通过、绑定失败"或者两个服务抢端口。
    """
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _free_port() -> int:
    for offset in range(PORT_TRIES):
        port = BASE_PORT + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            _use_exclusive_port(s)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"{BASE_PORT} 往后的 {PORT_TRIES} 个端口都被占了，先关掉一些程序。")


_BROWSER_NAMES = ("msedge.exe", "chrome.exe")
_BROWSER_FALLBACKS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_app_browser() -> str | None:
    """找一个能用应用模式开窗口的浏览器。"""
    try:
        import winreg
    except ImportError:
        winreg = None  # type: ignore[assignment]

    found: list[str] = []
    if winreg is not None:
        for exe in _BROWSER_NAMES:
            subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, subkey) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                if path and Path(path).is_file():
                    found.append(path)
    for path in _BROWSER_FALLBACKS:
        if Path(path).is_file():
            found.append(path)
    return found[0] if found else None


def app_profile_dir() -> Path:
    """应用专用的浏览器配置目录。

    Edge 已在后台运行时，新启动的 msedge.exe 只是给老进程发消息，
    命令行参数会被丢掉（包括 --start-fullscreen）。给一个独占的
    --user-data-dir 才能逼它开新进程。
    """
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    path = base / "study-assistant" / "browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_window(
    url: str, app_window: bool = True, fullscreen: bool = False
) -> tuple[str, int | None]:
    """打开界面。返回 (说明, 浏览器进程号)。"""
    import subprocess

    if app_window:
        exe = find_app_browser()
        if exe:
            try:
                target = f"{url}?fullscreen=1" if fullscreen else url
                args = [exe, f"--app={target}"]
                try:
                    args.append(f"--user-data-dir={app_profile_dir()}")
                except OSError:
                    pass
                args += ["--no-first-run", "--no-default-browser-check"]
                args.append("--start-fullscreen" if fullscreen else "--start-maximized")
                proc = subprocess.Popen(args, close_fds=True)
                how = f"独立窗口（{Path(exe).stem} 的应用模式）"
                return (f"{how}（全屏）" if fullscreen else how), proc.pid
            except OSError:
                pass
    webbrowser.open(url)
    return "普通浏览器标签页", None


def close_app_window(pid: int | None, title_hint: str = "个人学习助手") -> int:
    """关掉我们打开的那个浏览器窗口。找不到就返回 0，绝不抛异常。"""
    if os.name != "nt" or not pid:
        return 0

    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    by_pid: list[int] = []
    by_title: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if not cls.value.startswith("Chrome_WidgetWin"):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            by_pid.append(hwnd)
        else:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if title_hint in title.value:
                by_title.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    targets = by_pid or by_title
    for hwnd in targets:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return len(targets)


# --------------------------------------------------------------------------
# 请求体模型 —— 换 FastAPI 之后最直接的收益
# --------------------------------------------------------------------------
class ReviewIn(BaseModel):
    card_id: int
    rating: int = Field(ge=0, le=5)


class StageIn(BaseModel):
    card_id: int
    rating: int = Field(ge=0, le=5)
    raw_input: str = ""


class StagingIdIn(BaseModel):
    staging_id: int


class ProfileIn(BaseModel):
    key: str
    value: str


class TaskIn(BaseModel):
    title: str
    subject: str = ""
    due_date: str | None = None
    minutes: int = 30
    priority: int = 2


class TaskStatusIn(BaseModel):
    task_id: int
    # 用 Literal 而不是 str：非法状态在进业务层之前就被 422 拦掉，
    # 不用等到 repo 里抛 ValueError 变成 500。
    status: Literal["todo", "done"]


class FocusIn(BaseModel):
    planned_min: int
    actual_sec: int
    finished: bool = False
    task_id: int | None = None


class ExplainIn(BaseModel):
    card_id: int
    rating: int = Field(ge=0, le=5)


class ChatIn(BaseModel):
    question: str = ""


def build_client() -> ChatClient:
    """按 .env 建客户端。每次请求重建 —— 改完 key 立刻生效。"""
    settings = config.llm_settings()
    return ChatClient(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        model=settings["model"],
    )


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
def create_app(db_path: str | None = None) -> FastAPI:
    """组装 FastAPI 应用。测试里直接拿它喂 TestClient。"""
    app = FastAPI(title="个人学习助手", version="0.3.0")
    app.state.db_path = str(db_path or db.DEFAULT_DB)
    app.state.server = None          # uvicorn.Server，/api/shutdown 要用
    app.state.browser_pid = None     # 关窗口要用
    app.state.last_request = time.time()

    conn = db.connect(app.state.db_path)
    db.init_db(conn)
    seed.bootstrap(conn)
    conn.close()

    def open_conn() -> sqlite3.Connection:
        return db.connect(app.state.db_path)

    @app.exception_handler(LookupError)
    async def _lookup_error(_request: Request, exc: LookupError):
        # 前端统一按 {error: ...} 处理
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.middleware("http")
    async def _touch(request: Request, call_next):
        app.state.last_request = time.time()
        return await call_next(request)

    # ---- 页面 ----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(UI_PATH.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store"})

    # ---- 读接口 --------------------------------------------------------
    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        c = open_conn()
        try:
            return vm.state(c)
        finally:
            c.close()

    @app.get("/api/queue")
    async def queue():
        c = open_conn()
        try:
            return {"cards": vm.queue(c)}
        finally:
            c.close()

    @app.get("/api/stats")
    async def stats():
        c = open_conn()
        try:
            return vm.stats(c)
        finally:
            c.close()

    @app.get("/api/forecast")
    async def forecast(days: int = Query(7, ge=1, le=60)):
        c = open_conn()
        try:
            return {"days": vm.forecast(c, days)}
        finally:
            c.close()

    @app.get("/api/staged")
    async def staged():
        c = open_conn()
        try:
            return {"items": vm.staged(c)}
        finally:
            c.close()

    @app.get("/api/preview")
    async def preview(card_id: int = Query(0)):
        c = open_conn()
        try:
            return vm.card_preview(c, card_id)
        finally:
            c.close()

    @app.get("/api/dashboard")
    async def dashboard():
        c = open_conn()
        try:
            data = vm.dashboard(c)
            data["llm_ready"] = config.llm_configured()
            data["llm_model"] = config.llm_settings()["model"]
            return data
        finally:
            c.close()

    # ---- 写接口 --------------------------------------------------------
    @app.post("/api/review")
    async def review(body: ReviewIn):
        c = open_conn()
        try:
            result = repo.apply_review(c, card_id=body.card_id, rating=body.rating)
            return {"ok": True, **vm.review_result(result),
                    "remaining": len(repo.due_cards(c, limit=None))}
        finally:
            c.close()

    @app.post("/api/stage")
    async def stage(body: StageIn):
        c = open_conn()
        try:
            staging_id = repo.stage_review(
                c, card_id=body.card_id, rating=body.rating, raw_input=body.raw_input)
            return {"ok": True, "staging_id": staging_id}
        finally:
            c.close()

    @app.post("/api/confirm")
    async def confirm(body: StagingIdIn):
        c = open_conn()
        try:
            result = repo.confirm_staged(c, body.staging_id)
            return {"ok": True, **vm.review_result(result),
                    "remaining": len(repo.due_cards(c, limit=None))}
        finally:
            c.close()

    @app.post("/api/drop")
    async def drop(body: StagingIdIn):
        c = open_conn()
        try:
            repo.drop_staged(c, body.staging_id)
            return {"ok": True}
        finally:
            c.close()

    @app.post("/api/profile")
    async def profile(body: ProfileIn):
        c = open_conn()
        try:
            repo.set_profile(c, body.key, body.value)
            return {"ok": True, "profile": repo.get_profile(c)}
        finally:
            c.close()

    @app.post("/api/task/add")
    async def task_add(body: TaskIn):
        c = open_conn()
        try:
            task_id = repo.add_task(
                c, title=body.title, subject=body.subject,
                due_date=body.due_date, minutes=body.minutes,
                priority=body.priority)
            return {"ok": True, "task_id": task_id}
        finally:
            c.close()

    @app.post("/api/task/status")
    async def task_status(body: TaskStatusIn):
        c = open_conn()
        try:
            repo.set_task_status(c, body.task_id, body.status)
            return {"ok": True}
        finally:
            c.close()

    @app.post("/api/focus/log")
    async def focus_log(body: FocusIn):
        c = open_conn()
        try:
            session_id = repo.log_focus(
                c, planned_min=body.planned_min, actual_sec=body.actual_sec,
                finished=body.finished, task_id=body.task_id)
            return {"ok": True, "session_id": session_id}
        finally:
            c.close()

    @app.post("/api/explain")
    async def explain(body: ExplainIn):
        from study_assistant import coach

        c = open_conn()
        try:
            profile_data = repo.get_profile(c)
            return coach.explain_or_hint(
                c, build_client(), body.card_id,
                rating=body.rating, style=profile_data.get("style", ""))
        finally:
            c.close()

    # ---- Agent：普通返回 + SSE 流式 -------------------------------------
    @app.post("/api/agent/chat")
    async def agent_chat(body: ChatIn):
        from study_assistant import agent

        c = open_conn()
        try:
            try:
                result = agent.run(c, build_client(), body.question)
            except LLMError as exc:
                return {"ok": False, "reason": "llm_error",
                        "answer": f"模型那边出问题了：{exc}", "trace": []}
            return {"ok": True, **result}
        finally:
            c.close()

    @app.post("/api/agent/stream")
    async def agent_stream(body: ChatIn):
        """SSE：边跑边推事件，前端能看到"谁在跑、谁跑完了"。

        编排是同步的（模型调用是阻塞的），所以丢到线程里跑，
        事件通过 call_soon_threadsafe 送回事件循环。
        """
        from study_assistant import agent

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def push(event) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def work() -> None:
            c = open_conn()
            try:
                agent.run(c, build_client(), body.question, on_event=push)
            except LLMError as exc:
                push({"type": "error", "message": f"模型那边出问题了：{exc}"})
            except Exception as exc:      # noqa: BLE001
                push({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                c.close()
                push(None)               # 结束标记

        threading.Thread(target=work, daemon=True).start()

        async def generate() -> AsyncIterator[str]:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/shutdown")
    async def shutdown():
        server = app.state.server
        if server is not None:
            def later() -> None:
                time.sleep(0.4)          # 先让这个响应回到页面
                server.should_exit = True
            threading.Thread(target=later, daemon=True).start()
        return {"ok": True, "message": "应用已关闭"}

    return app


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def serve(
    db_path=None,
    port: int | None = None,
    open_browser: bool = True,
    app_window: bool = True,
    fullscreen: bool = False,
) -> None:
    app = create_app(db_path)
    chosen = port or _free_port()
    url = f"http://127.0.0.1:{chosen}/"

    # pythonw 启动时 sys.stdout 是 None，而 uvicorn 默认的日志配置会调用
    # sys.stdout.isatty() —— 直接崩。没有控制台时就别让它去配日志。
    # （这是从 http.server 换过来才出现的问题：原来那套根本不碰 stdout。）
    log_config = None if sys.stdout is None else uvicorn.config.LOGGING_CONFIG
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=chosen, log_level="warning",
        access_log=False, log_config=log_config))
    app.state.server = server

    print()
    print("-" * 64)
    print("  个人学习助手 已经启动")
    print("-" * 64)
    print(f"  界面地址 : {url}")
    print(f"  接口文档 : {url}docs")
    print(f"  数据库   : {app.state.db_path}")
    print()
    print(f"  正在打开：{'独立应用窗口' if app_window else '浏览器标签页'}")
    print("  想关掉它：点界面左下角的「关闭应用」")
    print("-" * 64)
    print()

    if open_browser:
        def _open() -> None:
            time.sleep(0.8)              # 等 uvicorn 真正开始监听
            how, pid = open_window(url, app_window=app_window, fullscreen=fullscreen)
            app.state.browser_pid = pid
            print(f"  已用「{how}」打开。")

        threading.Thread(target=_open, daemon=True).start()

    def _watch_idle() -> None:
        while not server.should_exit:
            time.sleep(30)
            if time.time() - app.state.last_request > IDLE_TIMEOUT_SECONDS:
                print("\n  太久没人用了，自动退出。")
                server.should_exit = True

    threading.Thread(target=_watch_idle, daemon=True).start()

    try:
        server.run()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        pid = app.state.browser_pid
        if pid:
            time.sleep(0.5)              # 让页面先把"应用已关闭"画出来
            closed = close_app_window(pid)
            print(f"  已关闭应用窗口（{closed} 个）。" if closed
                  else "  没找到应用窗口，需要手动关掉。")


def _fatal(message: str) -> None:
    """启动失败时一定要让用户看见 —— 哪怕没有控制台窗口。"""
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "个人学习助手 启动失败", 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="study-web", description="个人学习助手 网页界面")
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    parser.add_argument("--port", type=int, default=0, help="不填就自动挑一个空闲端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--plain-browser", action="store_true",
                        help="用普通浏览器标签页打开，而不是独立应用窗口")
    parser.add_argument("--fullscreen", action="store_true",
                        help="全屏打开（退出要按 F11，默认是最大化窗口）")
    args = parser.parse_args()

    silent = args.no_browser or bool(os.environ.get("STUDY_COACH_NO_BROWSER"))
    try:
        serve(
            db_path=args.db,
            port=args.port or None,
            open_browser=not silent,
            app_window=not args.plain_browser,
            fullscreen=args.fullscreen,
        )
    except OSError as exc:
        _fatal(f"启动失败：{exc}\n\n试试先关掉几个占用端口的程序，再重新打开。")
        raise SystemExit(1) from exc
