"""本地网页界面（Phase 3 提前做了一半）。

只用标准库：http.server 起一个小服务器，浏览器负责画界面。
这样 Phase 1 的"零依赖"这个特点保住了，也顺便讲清楚一件事——
**网页应用 = 一个能返 HTML 的服务器 + 一个浏览器。** 没有魔法。

启动：
    python -m study_assistant.web
然后浏览器会自动打开 http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from study_assistant import config, db, repo, seed
from study_assistant import view_model as vm
from study_assistant.llm import ChatClient

try:  # 只有 Windows 才有，导入失败就算了
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

UI_PATH = Path(__file__).with_name("ui.html")
BASE_PORT = 8765
PORT_TRIES = 12

# 没人用多久之后自动退出。
# 为什么需要这个：双击启动时我们没有控制台窗口可以关，
# 如果用户直接叉掉浏览器标签页，服务器就会变成一个"看不见的孤儿进程"。
IDLE_TIMEOUT_SECONDS = 2 * 60 * 60


# --------------------------------------------------------------------------
# 每个接口要返回的数据
# --------------------------------------------------------------------------
# 真正的查询都在 view_model.py 里——因为不止网页在用它们，
# 终端菜单和原生窗口要的是同一批数据。这里只起个别名，让下面的代码读起来短些。
_state = vm.state
_queue = vm.queue
_stats = vm.stats
_staged = vm.staged
_preview = vm.card_preview


def _forecast(conn: sqlite3.Connection, days: int) -> dict:
    return {"days": vm.forecast(conn, days)}


def _use_exclusive_port(sock: socket.socket) -> None:
    """让这个 socket 独占端口。

    这里有个 Windows 特有的坑：Linux 上 SO_REUSEADDR 的意思是"可以在
    TIME_WAIT 状态下重新绑"，而 Windows 上它的意思是"允许别人绑我已经占了
    的端口"。后果是——探测端口时永远显示"空闲"，最后两个服务抢一个端口，
    请求被随机分给其中一个，表现就是"数据时好时坏"，极难排查。

    Windows 的正解是 SO_EXCLUSIVEADDRUSE。
    """
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


class StudyServer(ThreadingHTTPServer):
    """默认的 ThreadingHTTPServer 会在 Windows 上打开 SO_REUSEADDR，
    所以我们自己接管绑定过程。"""

    daemon_threads = True          # 停服务时不要卡在没结束的请求上
    allow_reuse_address = False    # 由 server_bind 自己决定怎么设

    def server_bind(self) -> None:
        _use_exclusive_port(self.socket)
        super().server_bind()


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "study-assistant"

    # ---- 小工具：发 JSON / 发文件 -------------------------------------
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json({"error": f"找不到文件：{path.name}"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 不缓存：改完 ui.html 刷新浏览器就能看到，方便边学边改
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # 每个请求单独开一条连接：sqlite 的连接不能跨线程共用
    def _conn(self) -> sqlite3.Connection:
        # 记一下"最近一次有人用"的时间，空闲太久就自己退出
        self.server.last_request = time.time()  # type: ignore[attr-defined]
        return db.connect(self.server.db_path)  # type: ignore[attr-defined]

    def _offline(self, message: str) -> None:
        """把服务关掉——先让回复发出去，再在后台线程里停。"""
        self._json({"ok": True, "message": message})
        threading.Thread(target=self._shutdown_soon, daemon=True).start()

    def _shutdown_soon(self) -> None:
        # 等一会儿再停：让 /api/shutdown 的响应先回到页面上，
        # 页面才有机会把"正在退出"画出来，而不是直接变成连接错误。
        time.sleep(0.4)
        self.server.shutdown()  # type: ignore[attr-defined]

    # ---- GET ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        conn = self._conn()
        try:
            if path in ("/", "/index.html"):
                self._file(UI_PATH, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(_state(conn))
            elif path == "/api/queue":
                self._json({"cards": _queue(conn)})
            elif path == "/api/stats":
                self._json(_stats(conn))
            elif path == "/api/forecast":
                days = int((query.get("days") or ["7"])[0])
                self._json(_forecast(conn, max(1, min(days, 60))))
            elif path == "/api/staged":
                self._json({"items": _staged(conn)})
            elif path == "/api/preview":
                card_id = int((query.get("card_id") or ["0"])[0])
                self._json(_preview(conn, card_id))
            elif path == "/api/ping":
                self._json({"ok": True})
            elif path == "/api/dashboard":
                data = vm.dashboard(conn)
                # 界面靠这个决定要不要显示「让 AI 讲一遍」
                data["llm_ready"] = config.llm_configured()
                data["llm_model"] = config.llm_settings()["model"]
                self._json(data)
            else:
                self._json({"error": f"没有这个接口：{path}"}, 404)
        except LookupError as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:  # 兜底：别让界面白屏
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()

    # ---- POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload = self._body()
        conn = self._conn()
        try:
            if path == "/api/review":
                card_id = int(payload["card_id"])
                rating = int(payload["rating"])
                if not 0 <= rating <= 5:
                    self._json({"error": "评分必须是 0-5 的整数"}, 400)
                    return
                result = repo.apply_review(conn, card_id=card_id, rating=rating)
                self._json({**_review_result(result), "remaining": len(repo.due_cards(conn, limit=None))})
            elif path == "/api/stage":
                staging_id = repo.stage_review(
                    conn,
                    card_id=int(payload["card_id"]),
                    rating=int(payload["rating"]),
                    raw_input=str(payload.get("raw_input", "")),
                )
                self._json({"ok": True, "staging_id": staging_id})
            elif path == "/api/confirm":
                result = repo.confirm_staged(conn, int(payload["staging_id"]))
                self._json({**_review_result(result), "remaining": len(repo.due_cards(conn, limit=None))})
            elif path == "/api/drop":
                repo.drop_staged(conn, int(payload["staging_id"]))
                self._json({"ok": True})
            elif path == "/api/profile":
                repo.set_profile(conn, str(payload["key"]), str(payload["value"]))
                self._json({"ok": True, "profile": repo.get_profile(conn)})
            elif path == "/api/task/add":
                task_id = repo.add_task(
                    conn,
                    title=str(payload["title"]),
                    subject=str(payload.get("subject", "")),
                    due_date=payload.get("due_date") or None,
                    minutes=int(payload.get("minutes", 30)),
                    priority=int(payload.get("priority", 2)),
                )
                self._json({"ok": True, "task_id": task_id})
            elif path == "/api/task/status":
                repo.set_task_status(conn, int(payload["task_id"]), str(payload["status"]))
                self._json({"ok": True})
            elif path == "/api/focus/log":
                session_id = repo.log_focus(
                    conn,
                    planned_min=int(payload["planned_min"]),
                    actual_sec=int(payload["actual_sec"]),
                    finished=bool(payload.get("finished", False)),
                    task_id=payload.get("task_id") or None,
                )
                self._json({"ok": True, "session_id": session_id})
            elif path == "/api/explain":
                # 延迟导入：coach 只在真正要用到模型的时候才需要
                from study_assistant import coach

                profile = repo.get_profile(conn)
                self._json(coach.explain_or_hint(
                    conn,
                    build_client(),
                    int(payload["card_id"]),
                    rating=int(payload["rating"]),
                    style=profile.get("style", ""),
                ))
            elif path == "/api/shutdown":
                self._offline("应用已关闭")
            else:
                self._json({"error": f"没有这个接口：{path}"}, 404)
        except LookupError as exc:
            self._json({"error": str(exc)}, 404)
        except KeyError as exc:
            self._json({"error": f"缺少参数：{exc}"}, 400)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()

    # 控制台不打访问日志，只留启动信息，免得刷屏
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return


def _review_result(result: dict) -> dict:
    return {"ok": True, **vm.review_result(result)}


def build_client() -> ChatClient:
    """按 .env / 环境变量里的配置建一个大模型客户端。

    每次请求重建一个 —— 它只是个没有连接的普通对象，
    比维护一个全局单例简单，也不会在改完 .env 之后还用着旧 key。
    """
    settings = config.llm_settings()
    return ChatClient(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        model=settings["model"],
    )


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def _free_port() -> int:
    """从 8765 开始往后找一个没人占的端口。"""
    for offset in range(PORT_TRIES):
        port = BASE_PORT + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            _use_exclusive_port(s)   # 关键：不然"探测"永远说端口是空的
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"{BASE_PORT} 往后的 {PORT_TRIES} 个端口都被占了，先关掉一些程序。")


# --------------------------------------------------------------------------
# 打开窗口
# --------------------------------------------------------------------------
# 用 Edge / Chrome 的「应用模式」打开：没有地址栏、没有标签页、没有后退按钮，
# 看起来就是一个独立的软件窗口。界面代码一行都不用改。
_BROWSER_NAMES = ("msedge.exe", "chrome.exe")
_BROWSER_FALLBACKS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_app_browser() -> str | None:
    """找一个能用应用模式开窗口的浏览器，找不到返回 None。"""
    found: list[str] = []

    # 先问注册表——这是最可靠的，用户装在哪它都知道
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

    # 注册表没有就翻常见目录兜底
    for path in _BROWSER_FALLBACKS:
        if Path(path).is_file():
            found.append(path)

    return found[0] if found else None


def app_profile_dir() -> Path:
    """应用专用浏览器配置目录。

    为什么要这个东西：Edge 已经在后台运行的时候，再启动 msedge.exe 只是
    "给老进程发一条消息说再开个窗口"，命令行参数（包括 --start-fullscreen）
    会被直接丢掉，新窗口按老进程的默认设置开。

    给它一个**独占的 --user-data-dir**，Edge 就没法复用老进程，只能新起一个，
    参数才真正生效。顺带还有两个好处：
      · 任务栏图标不和你的个人 Edge 挤在一起，更像一个独立软件；
      · 关掉窗口这个进程就退出，不会留一堆后台。
    """
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    path = base / "study-assistant" / "browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_window(
    url: str, app_window: bool = True, fullscreen: bool = False
) -> tuple[str, int | None]:
    """打开界面。

    Returns:
        (说明文字, 浏览器进程号)。进程号用来在「关闭应用」时把窗口也关掉，
        所以只有"我们自己起的应用窗口"才会返回它。
    """
    if app_window:
        exe = find_app_browser()
        if exe:
            try:
                # 带上 ?fullscreen=1 让页面知道"这次想全屏"。
                # 页面会埋伏一个监听：用户第一次点任何地方就进全屏。
                # 为什么不全靠 --start-fullscreen：Edge 已经在后台跑的时候，
                # 命令行参数根本不会被读，窗口还是带标题栏。
                target = f"{url}?fullscreen=1" if fullscreen else url
                args = [exe, f"--app={target}"]
                try:
                    # 独占配置目录 —— 这是"默认全屏"能不能成立的关键。
                    # 万一建不出来（没权限、盘满了），也只是回到"带标题栏"，
                    # 不该因此连应用窗口都不开。
                    args.append(f"--user-data-dir={app_profile_dir()}")
                except OSError:
                    pass
                args += ["--no-first-run", "--no-default-browser-check"]
                if fullscreen:
                    # --start-fullscreen 比 --kiosk 温和：没有标题栏，
                    # 但还留着 F11 / Esc 可以退出来，不会把用户困住。
                    args.append("--start-fullscreen")
                else:
                    # 最大化而不是固定尺寸：铺满屏幕、只留任务栏，
                    # 和全屏看起来几乎一样，但可以随便拖、缩、关。
                    # 全屏一旦由浏览器接管，页面就失去了控制权 ——
                    # 用户想退出来只能按 F11，这是个糟糕的默认值。
                    args.append("--start-maximized")
                proc = subprocess.Popen(args, close_fds=True)
                how = f"独立窗口（{Path(exe).stem} 的应用模式）"
                return (f"{how}（全屏）" if fullscreen else how), proc.pid
            except OSError:
                pass  # 打不开就退回普通浏览器
    webbrowser.open(url)
    # 用你自己的浏览器打开的，不能说关就关 —— 所以不返回进程号
    return "普通浏览器标签页", None


def close_app_window(pid: int | None, title_hint: str = "个人学习助手") -> int:
    """把应用窗口关掉，返回关了几个。

    为什么需要这个：全屏是浏览器管的，网页里点不动它 ——
    所以「关闭应用」这个按钮只能从外面把窗口关掉。
    而且它本来就应该关窗口：只停后台、留一个死窗口在那儿，那不叫关闭应用。

    先按进程号找，找不到就用窗口标题兜底（启动器进程和真正的浏览器进程
    偶尔不是同一个）。
    """
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


def make_server(db_path=None, port: int = 0) -> ThreadingHTTPServer:
    """把服务器搭起来但不阻塞。测试里用它，serve() 也用它。"""
    conn = db.connect(db_path or db.DEFAULT_DB)
    db.init_db(conn)
    seed.bootstrap(conn)
    conn.close()

    # port=0 让操作系统随便挑一个空闲端口（测试里很有用）
    httpd = StudyServer(("127.0.0.1", port or _free_port()), Handler)
    httpd.db_path = db_path or db.DEFAULT_DB  # type: ignore[attr-defined]
    httpd.last_request = time.time()  # type: ignore[attr-defined]
    return httpd


def _watch_idle(httpd: ThreadingHTTPServer, timeout: float = IDLE_TIMEOUT_SECONDS) -> None:
    """后台盯着"多久没人用了"，超时就自己关掉。"""
    while True:
        time.sleep(60)
        idle = time.time() - httpd.last_request  # type: ignore[attr-defined]
        if idle > timeout:
            print(f"\n  已经 {idle / 60:.0f} 分钟没人用了，自动退出。")
            httpd.shutdown()
            return


def serve(
    db_path=None,
    port: int | None = None,
    open_browser: bool = True,
    app_window: bool = True,
    fullscreen: bool = False,
) -> None:
    httpd = make_server(db_path, port or 0)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"

    # pythonw.exe 启动时 sys.stdout 是 None，这些 print 会自动变成空操作
    print()
    print("─" * 64)
    print("  个人学习助手 已经启动")
    print("─" * 64)
    print(f"  界面地址 : {url}")
    print(f"  数据库   : {httpd.db_path}")  # type: ignore[attr-defined]
    print()
    print(f"  正在打开：{'独立应用窗口' if app_window else '浏览器标签页'}"
          f"{'（全屏）' if fullscreen else ''}")
    print("  想关掉它：点界面右上角的「关闭应用」")
    print("─" * 64)
    print()

    if open_browser:
        def _open() -> None:
            time.sleep(0.6)  # 等服务器先真正开始监听
            how, pid = open_window(url, app_window=app_window, fullscreen=fullscreen)
            httpd.browser_pid = pid          # type: ignore[attr-defined]
            print(f"  已用「{how}」打开。")

        threading.Thread(target=_open, daemon=True).start()

    threading.Thread(target=_watch_idle, args=(httpd,), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()
        # 「关闭应用」发生时，顺手把窗口也关掉 —— 只停后台会留下一个死窗口，
        # 而且全屏是浏览器管的，页面自己退不出来，这个出口必须有。
        pid = getattr(httpd, "browser_pid", None)
        if pid:
            time.sleep(0.5)              # 让页面先把"应用已关闭"画出来
            closed = close_app_window(pid)
            print(f"  已关闭应用窗口（{closed} 个）。" if closed
                  else "  没找到应用窗口，需要手动关掉。")


def _fatal(message: str) -> None:
    """启动失败时，让用户一定看得见——哪怕没有控制台窗口。"""
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
    parser.add_argument(
        "--plain-browser",
        action="store_true",
        help="用普通浏览器标签页打开，而不是独立应用窗口",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="用全屏方式打开（没有标题栏），按 F11 或 Esc 可以退出全屏",
    )
    args = parser.parse_args()
    # 环境变量也行：方便自动化脚本里跑，不会突然弹一个浏览器出来
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
        # 最常见的就是端口全被占了。没有控制台的话，弹个对话框总比默默死掉好。
        _fatal(f"启动失败：{exc}\n\n试试先关掉几个占用端口的程序，再重新打开。")
        raise SystemExit(1) from exc
