"""原生桌面界面（tkinter）。

tkinter 是 Python 标准库自带的，所以这个界面**零下载**——
不用 pip install 任何东西，`dependencies = []` 这条底线保住了。

这是 study-coach 的第四个壳：

    cli.py       一条命令做一件事（适合脚本化）
    session.py   终端里跑菜单
    web.py       网页服务器 + 浏览器
    desktop.py   原生窗口  ← 这个文件

四个壳共用 sm2.py / repo.py / view_model.py，业务逻辑一行都不用改。
这就是当初把"查数据"抽到 view_model 的回报。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

from study_coach import db, repo, view_model as vm
from study_coach import seed

# --------------------------------------------------------------------------
# 配色。和网页版是同一套，这样两个界面看起来是一个产品。
# --------------------------------------------------------------------------
BG = "#0d1117"
PANEL = "#161b22"
PANEL2 = "#1c2128"
LINE = "#30363d"
FG = "#e6edf3"
DIM = "#8b949e"
GREEN = "#3fb950"
BLUE = "#58a6ff"
YELLOW = "#d29922"
RED = "#f85149"
PURPLE = "#bc8cff"

FONT = "Microsoft YaHei UI"
F_TITLE = (FONT, 13, "bold")
F_SUB = (FONT, 10)
F_HEAD = (FONT, 11, "bold")
F_Q = (FONT, 17)
F_BODY = (FONT, 11)
F_SMALL = (FONT, 9)
F_BTN = (FONT, 10)
F_RATE = (FONT, 13, "bold")

RATING_LABEL = {5: "完美", 4: "有点犹豫", 3: "很吃力",
                2: "错了但想起", 1: "错了但记得", 0: "完全不会"}


# 图标：圆角方块 + 祖母绿渐变 + 白色沙漏。
# icon.ico 里含 16/32/48/64/128/256 六种尺寸，Windows 会按用途自己挑
# （任务栏用 32，Alt+Tab 用 48，桌面大图标用 256），所以每种尺寸下都清楚。
# 生成脚本在 work/make_app_icon.py —— 纯标准库手写 SDF + 抗锯齿，没用图形库。
ICON_ICO = Path(__file__).with_name("icon.ico")
ICON_PNG = Path(__file__).with_name("icon.png")


def _btn(parent: tk.Misc, text: str, command, **kw) -> tk.Button:
    """统一样式的按钮。tkinter 默认按钮在 Windows 上很"系统"，这里全部自己调。"""
    opts = dict(
        text=text, command=command,
        bg=PANEL2, fg=FG, activebackground=LINE, activeforeground=FG,
        font=F_BTN, relief="flat", bd=0, highlightthickness=0,
        padx=14, pady=7, cursor="hand2",
    )
    opts.update(kw)
    return tk.Button(parent, **opts)


class StudyApp:
    def __init__(self, conn, db_path: str, visible: bool = True):
        self.conn = conn
        self.db_path = db_path

        # 界面状态
        self.tab = "review"
        self.index = 0          # 复习到第几张
        self.revealed = False   # 答案露出来了吗
        self.toast: tuple[str, bool] | None = None
        self.staged_mode = False
        self.done = 0
        self.right = 0

        self.root = tk.Tk()
        self.root.title("study-coach · 备考助手")
        self.root.geometry("1020x730")
        self.root.minsize(860, 620)
        self.root.configure(bg=BG)
        if not visible:
            # 测试的时候别真弹一个窗口出来
            self.root.withdraw()
        self._set_icon()

        self._build_header()
        self._build_nav()
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=24, pady=(14, 18))

        self._bind_keys()
        self.reload()

    # ---------------------------------------------------------------- 骨架
    def _set_icon(self) -> None:
        """设置窗口 / 任务栏图标。图标只是装饰，任何一步失败都直接跳过。"""
        if ICON_ICO.is_file():
            try:
                self.root.iconbitmap(default=str(ICON_ICO))
                return
            except tk.TclError:
                pass
        try:
            if ICON_PNG.is_file():
                image = tk.PhotoImage(file=str(ICON_PNG))
                self.root.iconphoto(True, image)
                self._icon_ref = image     # 必须留个引用，不然会被回收掉
        except tk.TclError:
            pass

    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=PANEL)
        head.pack(fill="x")

        inner = tk.Frame(head, bg=PANEL, height=60)
        inner.pack(fill="x")
        inner.pack_propagate(False)

        tk.Label(inner, text="study-coach", bg=PANEL, fg=FG, font=F_TITLE).pack(
            side="left", padx=(24, 8))
        tk.Label(inner, text="备考助手", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")

        self.headline = tk.Label(inner, text="", bg=PANEL, fg=DIM, font=F_SMALL)
        self.headline.pack(side="right", padx=(8, 16))

        _btn(inner, "关闭应用", self.close, padx=10, pady=4, font=F_SMALL).pack(
            side="right", padx=(0, 20), pady=14)

        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x")

    def _build_nav(self) -> None:
        nav = tk.Frame(self.root, bg=PANEL)
        nav.pack(fill="x")
        self.nav_buttons: dict[str, tk.Button] = {}
        for key, label in (("review", "复习"), ("stats", "统计"),
                           ("load", "负载"), ("profile", "画像")):
            b = tk.Button(
                nav, text=label, command=lambda k=key: self.show_tab(k),
                bg=PANEL, fg=DIM, activebackground=PANEL, activeforeground=FG,
                font=F_BODY, relief="flat", bd=0, highlightthickness=0,
                padx=18, pady=10, cursor="hand2",
            )
            b.pack(side="left")
            self.nav_buttons[key] = b
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x")

    def _bind_keys(self) -> None:
        """键盘快捷键，和网页版保持一致：空格看答案，0-5 打分。"""
        self.root.bind("<space>", self._on_space)
        for n in range(6):
            self.root.bind(str(n), lambda _e, n=n: self._on_digit(n))

    def _typing_in_entry(self) -> bool:
        return isinstance(self.root.focus_get(), (tk.Entry, tk.Text))

    def _on_space(self, _event) -> None:
        if self._typing_in_entry() or self.tab != "review" or self.revealed:
            return
        if self.queue:
            self.revealed = True
            self._render_review()

    def _on_digit(self, rating: int) -> None:
        if self._typing_in_entry() or self.tab != "review" or not self.revealed:
            return
        self.rate(rating)

    # ------------------------------------------------------------ 数据刷新
    def reload(self) -> None:
        self.state = vm.state(self.conn)
        self.queue = vm.queue(self.conn)
        self.stats = vm.stats(self.conn)
        self.forecast = vm.forecast(self.conn, 7)
        self.staged = vm.staged(self.conn)
        if self.index > len(self.queue):
            self.index = len(self.queue)
        self.render()

    def render(self) -> None:
        s = self.state
        text = f"{s['today']} · 到期 {s['due_total']} 张"
        if s["overdue"]:
            text += f" · 逾期 {s['overdue']} 张"
        text += f" · 今天已复习 {s['reviewed_today']} 次"
        self.headline.configure(text=text)
        for key, button in self.nav_buttons.items():
            on = key == self.tab
            button.configure(fg=FG if on else DIM,
                             bg=PANEL2 if on else PANEL,
                             font=(FONT, 11, "bold") if on else F_BODY)
        self._render_body()

    def show_tab(self, key: str) -> None:
        self.tab = key
        self.render()

    def _clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def _render_body(self) -> None:
        self._clear()
        if self.tab == "review":
            self._render_review()
        elif self.tab == "stats":
            self._render_stats()
        elif self.tab == "load":
            self._render_load()
        else:
            self._render_profile()

    # ------------------------------------------------------------ 卡片容器
    def _card(self, parent=None) -> tk.Frame:
        """带边框的"卡片"，网页版里那个圆角方块在 tkinter 里的对应物。"""
        frame = tk.Frame(parent or self.body, bg=PANEL,
                         highlightbackground=LINE, highlightthickness=1)
        frame.pack(fill="x", pady=(0, 14))
        return frame

    # ---------------------------------------------------------------- 复习
    def _render_review(self) -> None:
        card = self._card()
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", padx=22, pady=18)

        total = len(self.queue)
        # 进度条：用两个 Frame 叠出来，省得引 Canvas
        track = tk.Frame(inner, bg=LINE, height=6)
        track.pack(fill="x", pady=(0, 12))
        if total:
            pct = max(0.0, min(1.0, self.index / total))
            fill = tk.Frame(track, bg=GREEN, height=6)
            fill.place(x=0, y=0, relwidth=pct, relheight=1)

        tk.Label(inner, text=f"{min(self.index + (1 if self.index < total else 0), total)}"
                             f" / {total}",
                 bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="e")

        if total == 0:
            msg = (f"今天的 {self.state['reviewed_today']} 张都过完了，休息一下吧。"
                   if self.state["reviewed_today"] else "今天没有到期的卡片。")
            tk.Label(inner, text=msg, bg=PANEL, fg=DIM, font=F_BODY).pack(pady=30)
            self._render_mode_row(inner)
            self._render_staged()
            return

        current = self.queue[self.index]
        tk.Label(inner, text=f"{current['subject']} · {current['knowledge_point']}",
                 bg=PANEL, fg=PURPLE, font=F_SMALL).pack(anchor="w")
        tk.Label(inner, text=current["question"], bg=PANEL, fg=FG, font=F_Q,
                 justify="left", anchor="w", wraplength=self._wrap()).pack(
            anchor="w", pady=(6, 4))
        tk.Label(inner,
                 text=f"间隔 {current['interval_days']} 天 · EF {current['ef']}"
                      f" · 答错 {current['lapses']} 次",
                 bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="w")

        if not self.revealed:
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x", pady=(18, 0))
            _btn(row, "显示答案", self._reveal, bg="#238636", font=(FONT, 11, "bold")).pack(side="left")
            tk.Label(row, text="  先自己回忆一遍，再点这里（或按空格）",
                     bg=PANEL, fg=DIM, font=F_SMALL).pack(side="left")
        else:
            answer = tk.Frame(inner, bg=PANEL2)
            answer.pack(fill="x", pady=(16, 0))
            tk.Frame(answer, bg=GREEN, width=3).pack(side="left", fill="y")
            tk.Label(answer, text=current["answer"], bg=PANEL2, fg=FG,
                     font=(FONT, 11), justify="left", anchor="w",
                     wraplength=self._wrap() - 40).pack(
                side="left", padx=14, pady=12)
            if current["source"]:
                tk.Label(inner, text=f"来源：{current['source']}",
                         bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="w", pady=(6, 0))

            tk.Label(inner, text="这次答得怎么样？", bg=PANEL, fg=DIM,
                     font=F_SMALL).pack(anchor="w", pady=(16, 8))
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x")
            for n in (5, 4, 3, 2, 1, 0):
                color = GREEN if n >= 3 else (YELLOW if n >= 1 else RED)
                b = _btn(row, f"{n}\n{RATING_LABEL[n]}",
                         lambda n=n: self.rate(n),
                         fg=color, font=F_RATE, width=11, pady=8)
                b.pack(side="left", padx=(0, 8))
            note = ("逐步确认已打开：打分先进暂存区，确认后才生效。"
                    if self.staged_mode else "点分数就会立刻记录。也可以直接按数字键。")
            tk.Label(inner, text=note, bg=PANEL, fg=DIM, font=F_SMALL).pack(
                anchor="w", pady=(8, 0))

        if self.toast:
            text, bad = self.toast
            box = tk.Frame(inner, bg=PANEL2, highlightbackground=RED if bad else GREEN,
                           highlightthickness=1)
            box.pack(fill="x", pady=(14, 0))
            tk.Label(box, text=text, bg=PANEL2, fg=RED if bad else GREEN,
                     font=F_BODY, justify="left", anchor="w",
                     wraplength=self._wrap() - 40).pack(padx=14, pady=10, anchor="w")

        self._render_mode_row(inner)
        self._render_staged()

    def _render_mode_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=PANEL2)
        row.pack(fill="x", pady=(16, 0))
        var = tk.BooleanVar(value=self.staged_mode)

        def toggle() -> None:
            self.staged_mode = var.get()
            self.toast = None
            self._render_review()

        tk.Checkbutton(
            row, text="逐步确认模式（打分先进暂存区，我确认了才算）",
            variable=var, command=toggle,
            bg=PANEL2, fg=DIM, selectcolor=PANEL2,
            activebackground=PANEL2, activeforeground=FG,
            font=F_SMALL, bd=0, highlightthickness=0, cursor="hand2",
        ).pack(side="left", padx=12, pady=10)
        tk.Label(row, text="  默认直接记录。打开它是为了防止手滑——打错一次分，"
                           "这张卡的间隔会被重置。",
                 bg=PANEL2, fg=DIM, font=F_SMALL).pack(side="left")

    def _render_staged(self) -> None:
        if not self.staged:
            return
        card = self._card()
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=22, pady=16)
        tk.Label(inner, text=f"暂存区（{len(self.staged)} 条待确认）",
                 bg=PANEL, fg=FG, font=F_HEAD).pack(anchor="w")
        tk.Label(inner, text="这些打分还没写进记忆状态。确认才生效，丢掉就当作没打过。",
                 bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="w", pady=(2, 10))

        for item in self.staged:
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"{item['rating']} 分", bg=PANEL2, fg=BLUE,
                     font=F_SMALL, padx=8, pady=2).pack(side="left")
            tk.Label(row, text=item["question"][:44], bg=PANEL, fg=FG,
                     font=F_BODY, anchor="w").pack(side="left", padx=10)
            _btn(row, "丢掉", lambda i=item["id"]: self.drop_staged(i),
                 font=F_SMALL, padx=10, pady=3).pack(side="right")
            _btn(row, "确认", lambda i=item["id"]: self.confirm_staged(i),
                 font=F_SMALL, padx=10, pady=3).pack(side="right", padx=6)

    def _wrap(self) -> int:
        """正文的换行宽度：跟着窗口宽度走，窗口拉大文字也跟着变宽。"""
        width = self.body.winfo_width()
        return max(360, (width if width > 1 else 900) - 100)

    # ---------------------------------------------------------- 复习的动作
    def _reveal(self) -> None:
        self.revealed = True
        self._render_review()

    def rate(self, rating: int) -> None:
        if not self.queue or self.index >= len(self.queue):
            return
        card = self.queue[self.index]
        try:
            if self.staged_mode:
                repo.stage_review(self.conn, card_id=card["id"], rating=rating)
                self.toast = (f"已把「{rating} 分」放进暂存区。"
                              f"到下面的暂存区里确认或丢掉。", False)
            else:
                result = vm.review_result(
                    repo.apply_review(self.conn, card_id=card["id"], rating=rating))
                self.done += 1
                if rating >= 3:
                    self.right += 1
                    self.toast = (f"✓ 间隔 {result['before']['interval_days']} 天 → "
                                  f"{result['after']['interval_days']} 天，{result['note']}。", False)
                else:
                    self.toast = (f"✗ 答错，间隔重置为 "
                                  f"{result['after']['interval_days']} 天，这张卡会重新来过。", True)
            self.index += 1
            self.revealed = False
        except Exception as exc:                      # noqa: BLE001
            self.toast = (f"出错了：{exc}", True)
        self.reload()

    def confirm_staged(self, staging_id: int) -> None:
        try:
            result = vm.review_result(repo.confirm_staged(self.conn, staging_id))
            self.toast = (f"已确认：间隔 {result['before']['interval_days']} 天 → "
                          f"{result['after']['interval_days']} 天，下次 {result['next_due']}。", False)
        except LookupError as exc:
            self.toast = (str(exc), True)
        self.reload()

    def drop_staged(self, staging_id: int) -> None:
        try:
            repo.drop_staged(self.conn, staging_id)
            self.toast = ("已丢掉这条暂存，记忆状态原封不动。", False)
        except LookupError as exc:
            self.toast = (str(exc), True)
        self.reload()

    # ---------------------------------------------------------------- 统计
    def _render_stats(self) -> None:
        card = self._card()
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=22, pady=18)
        tk.Label(inner, text="各科统计", bg=PANEL, fg=FG, font=F_HEAD).pack(anchor="w")

        grid = tk.Frame(inner, bg=PANEL)
        grid.pack(fill="x", pady=(12, 0))
        heads = ("科目", "卡片", "今天到期", "平均间隔", "掌握度", "累计答错")
        for col, text in enumerate(heads):
            tk.Label(grid, text=text, bg=PANEL, fg=DIM, font=F_SMALL).grid(
                row=0, column=col, padx=10, pady=6, sticky="e" if col else "w")
        for r, row in enumerate(self.stats["subjects"], start=1):
            values = (row["subject"], row["total"], row["due_now"],
                      f"{row['avg_interval']:.1f}", f"{row['mastery']:.2f}", row["lapses"])
            for col, value in enumerate(values):
                tk.Label(grid, text=str(value), bg=PANEL, fg=FG, font=F_SMALL).grid(
                    row=r, column=col, padx=10, pady=5, sticky="e" if col else "w")
        tk.Label(inner, text="掌握度 = 1 / (1 + 平均间隔)。间隔越长说明记得越牢，越接近 1。",
                 bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="w", pady=(8, 0))

        card2 = self._card()
        inner2 = tk.Frame(card2, bg=PANEL)
        inner2.pack(fill="x", padx=22, pady=18)
        tk.Label(inner2, text="薄弱知识点", bg=PANEL, fg=FG, font=F_HEAD).pack(anchor="w")
        if self.stats["weak"]:
            for w in self.stats["weak"]:
                tk.Label(inner2,
                         text=f"· {w['knowledge_point']}    复习 {w['reviews']} 次，"
                              f"错 {w['wrongs']} 次",
                         bg=PANEL, fg=FG, font=F_BODY).pack(anchor="w", pady=2)
        else:
            tk.Label(inner2, text="还没有复习记录，看不出薄弱点。",
                     bg=PANEL, fg=DIM, font=F_BODY).pack(anchor="w", pady=6)
        tk.Label(inner2, text="这些都是 SQL 直接算出来的，没有一次大模型调用。",
                 bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="w", pady=(8, 0))

    # ---------------------------------------------------------------- 负载
    def _render_load(self) -> None:
        card = self._card()
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=22, pady=18)
        tk.Label(inner, text="未来 7 天的复习负载", bg=PANEL, fg=FG,
                 font=F_HEAD).pack(anchor="w")

        chart = tk.Canvas(inner, bg=PANEL, height=200, highlightthickness=0)
        chart.pack(fill="x", pady=(14, 0))
        self.root.update_idletasks()
        chart.update_idletasks()

        busy = max([d["count"] for d in self.forecast] + [1])
        width = max(400, self._wrap())
        label_w, bar_max = 92, max(120, width - 200)
        for i, day in enumerate(self.forecast):
            y = 12 + i * 26
            chart.create_text(label_w - 10, y + 7, text=day["date"], anchor="e",
                              fill=DIM, font=F_SMALL)
            chart.create_text(label_w + 6, y + 7, text=f"{day['count']} 张", anchor="w",
                              fill=FG, font=F_SMALL)
            length = int(day["count"] / busy * bar_max)
            chart.create_rectangle(0, y, bar_max, y + 14, fill=PANEL2, outline="")
            if length:
                chart.create_rectangle(0, y, length, y + 14, fill=BLUE, outline="")
        total = sum(d["count"] for d in self.forecast)
        tk.Label(inner, text=f"合计 {total} 张。这就是「我接下来几天会不会爆掉」的答案。",
                 bg=PANEL, fg=DIM, font=F_SMALL).pack(anchor="w", pady=(12, 0))

    # ---------------------------------------------------------------- 画像
    def _render_profile(self) -> None:
        card = self._card()
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=22, pady=18)
        tk.Label(inner, text="个人画像", bg=PANEL, fg=FG, font=F_HEAD).pack(anchor="w")

        entry: tk.Entry | None = None
        for key, value in self.state["profile"].items():
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x", pady=6)
            tk.Label(row, text=key, bg=PANEL, fg=DIM, font=F_BODY, width=16,
                     anchor="w").pack(side="left")
            if key == "daily_minutes":
                entry = tk.Entry(row, bg=PANEL2, fg=FG, insertbackground=FG,
                                 font=F_BODY, relief="flat", width=10)
                entry.insert(0, value)
                entry.pack(side="left", ipady=5, padx=(0, 8))
                _btn(row, "保存", self._save_daily, font=F_SMALL,
                     padx=10, pady=3).pack(side="left")
            else:
                tk.Label(row, text=value, bg=PANEL, fg=FG,
                         font=F_BODY, anchor="w").pack(side="left")

        self.daily_entry = entry
        if self.state["limit"]:
            tk.Label(inner,
                     text=f"由「每天 {self.state['profile'].get('daily_minutes', '?')} 分钟」"
                          f"推出：今天最多复习 {self.state['limit']} 张",
                     bg=PANEL, fg=FG, font=F_BODY).pack(anchor="w", pady=(10, 0))
        tk.Label(inner, text="画像字段是有限且确定的，所以这里直接查表，不走向量检索——\n"
                             "快，而且不会因为相似度差一点就漏掉。",
                 bg=PANEL, fg=DIM, font=F_SMALL, justify="left").pack(
            anchor="w", pady=(8, 0))

    def _save_daily(self) -> None:
        if self.daily_entry is None:
            return
        raw = self.daily_entry.get().strip()
        try:
            value = float(raw)
        except ValueError:
            self.toast = ("每天可用分钟数要填一个正数。", True)
            self._render_profile()
            return
        if value <= 0:
            self.toast = ("每天可用分钟数要填一个正数。", True)
            self._render_profile()
            return
        repo.set_profile(self.conn, "daily_minutes", raw)
        self.toast = (f"已保存：每天 {raw} 分钟，今天的题量上限跟着变了。", False)
        self.reload()

    # ---------------------------------------------------------------- 收尾
    def close(self) -> None:
        self.conn.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run(db_path: str | None = None) -> None:
    """入口：接上数据库，打开窗口。"""
    path = db_path or db.DEFAULT_DB
    conn = db.connect(path)
    db.init_db(conn)
    seed.bootstrap(conn)
    StudyApp(conn, str(path)).run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="study-desktop", description="study-coach 桌面界面")
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    run(parser.parse_args().db)
