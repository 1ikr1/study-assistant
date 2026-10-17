# 个人学习助手 · 项目约定

这个文件是写给**在这个项目里干活的 AI**的。用户在 Codex 里新开一个对话、
只要还在这个目录下，就会读到它 —— 所以跨对话的"记忆"靠的就是这里。

## 这是什么项目

个人备考助手。一句话定位：**模型负责理解，算法负责调度。**
「今天该复习什么」交给可复现的间隔重复算法（SM-2），模型只负责语言那一半。

它是用户的第二个作品。第一个是 `C:\Users\yv\Documents\Codex\Adaptive-rag`
（RAG 电商客服，FastAPI + LangGraph + 向量库），目标是 **Agent / AI 应用开发实习**。
所以两个项目的技术栈刻意错开：那边练重武器，这边练算法内核与交互，
并且**刻意保持零第三方依赖**。

## 硬性约定

1. **技术选型看场景适配度，不看守旧。**
   曾经有一条"保持零依赖"的规矩，**已经废除了** —— 那是一条自加的限制，
   不是用户的要求。用户的原话：*「零依赖不是底线，我更注重技术选型在
   这个项目场景下的适配度」*。

   所以每次引入/替换技术时问的是：**这东西解决的这个场景，它是不是最合适的？**
   代价（依赖体积、学习成本、迁移风险）要算，但不能拿"标准库能做"当理由
   而牺牲能力。反过来，能用标准库做得同样好并且更简单时，仍然倾向标准库。
2. **算和说分开。** 能用确定性代码算出来的，绝不交给大模型。
   复习调度、统计、负载预测全部是纯代码。目前全项目**只有一处**会调用模型：
   `coach.py` 的讲题，而且"要不要讲"也是算法判断（`should_explain`）。
3. **分层不许打穿。** 所有 SQL 只在 `repo.py`；界面要的数据只在 `view_model.py` 组装；
   `cli.py` / `session.py` / `web.py` / `desktop.py` 是四个"壳"，只负责画。
   改界面不许写 SQL，改算法不许碰界面。
4. **改完必须跑测试。** 见下面的命令。测试覆盖的是边界而不是覆盖率。
5. **注释和文档写中文**，并且解释"为什么这么做"而不是"这行干了什么"。
6. **API key 只放 `.env`**，永远不写进代码或文档。`.env` 已在 `.gitignore` 里，
   别把它挪出这个名单。

## 讲题那一块的约定

- `should_explain(rating)` 是**算法**判断，不是模型。答对了就不该发请求 ——
  测试里用假 transport 记账，确认一次都没发出去。
- `build_explain_messages()` 是**纯函数**。prompt 属于产品逻辑，
  改它要像改代码一样能测、能 review，不要把它藏在发请求的地方。
- 测大模型相关代码**一律注入假 transport**（`llm.py` 留了注入点）。
  真打网络的话，测试既慢又烧钱，错误分支还测不到。

## 命令

```powershell
cd C:\Users\yv\Documents\Codex\study-coach
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$py = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"

& $py -m unittest discover -s tests -v    # 跑全部测试
& $py -m study_assistant.web                  # 网页工作台（默认入口）
& $py -m study_assistant.desktop              # tkinter 版
.\study.cmd stats                         # 命令行版
```

## 已知的坑（都是真踩过的，别再踩一遍）

### 框架相关

- **pythonw 下 uvicorn 会崩**：它的默认日志配置里有 `sys.stdout.isatty()`，
  而 pythonw 的 `sys.stdout` 是 `None`。`serve()` 里已经按
  `log_config = None if sys.stdout is None else ...` 处理，别再改回去。
- **LangGraph 的 State 默认整体替换**：`results` 这种要累加的字典必须给
  `Annotated[dict, reducer]`，否则并行分支会互相覆盖。
- **sqlite 连接不能跨线程**：图里的节点会在线程池里跑，所以每个节点
  `db.connect(path)` 自己开连接，别把主线程的 conn 传进去。
- 沙箱里 pip 需要 `PYTHONPATH` 指一个改了 `tempfile.mkdtemp` 的垫片，
  并且要清掉 `PIP_NO_INDEX`、设 `HTTPS_PROXY`。这是本机调试用的，不影响项目。

### Windows 环境

- **`SO_REUSEADDR` 在 Windows 上语义不同**：它允许两个进程绑同一个端口。
  端口探测必须用 `SO_EXCLUSIVEADDRUSE`，否则会静默开出两个抢端口的服务，
  表现是"数据时好时坏"。见 `web.py` 的 `_use_exclusive_port`。
- **`tempfile` 建的目录默认 `0o700`**，在不允许 `chmod` 的环境里会写不进去。
  测试里改成自己 `mkdir`，见 `tests/test_repo.py`。
- **`.cmd` 文件必须是纯 ASCII**，一个中文就会让整个文件解析错位；换行得是 CRLF。
- **`.ps1` 要带 UTF-8 BOM**，否则 PowerShell 5.1 按 GBK 读会乱码。
- **PowerShell 执行策略是 Restricted**，`npm.ps1` 那类脚本会被拒；用 `.cmd` 绕。

### 浏览器壳（`web.py`）

- **Edge 已经在后台运行时，新启动的 `msedge.exe` 只是给老进程发消息**，
  命令行参数（含 `--start-fullscreen`）会被直接丢掉。必须给一个独占的
  `--user-data-dir`，逼它开新进程，参数才生效。
- **全屏有两种，谁在管完全不一样**：页面申请的（`fullscreenElement` 有值，
  Esc 能退）和浏览器自己的（`fullscreenElement` 是 null，**页面退不出去**）。
  界面上不要提供"全屏"按钮 —— 用户分不清这两种，按钮标签会骗人，
  结果就是卡在全屏里出不来。所以：窗口默认 `--start-maximized`，
  要全屏按 F11（浏览器自己的开关，对称）。
- **关闭应用 = 停后台 + 关窗口**，两者缺一不可。只停后台会留一个死窗口，
  那不叫关闭应用。关窗口靠 `EnumWindows` + `WM_CLOSE`，见 `close_app_window`。

### 设计上的教训

- **人在回路要防重复确认**：暂存区的 `confirm` 用条件 UPDATE
  （`WHERE id=? AND status='pending'` + `rowcount`）做原子抢占。
- **`review_logs` 必须记当时的算法状态**（`ef_before` / `interval_before` /
  `elapsed_days` / `algorithm`），否则算法升级后没法用历史重放对比。
- **EF 惩罚刻意不对称**：答对最多 +0.10，答错最多 −0.80，下限 1.3。
  这是保守设计，别"优化"成对称的。

## 当前进度与下一步

已完成：SM-2 内核、9 张表、四个壳（CLI / 终端菜单 / 网页工作台 / tkinter）、
人在回路、计划任务、专注计时、学习洞察、深浅主题、多分辨率图标、
「让 AI 讲一遍」、**多 Agent 编排层（Supervisor-Worker + 代码门控）**。157 个测试。

下一步还在 Phase 2 里，按用户偏好挑一个做：

1. **错题转卡片** —— 粘一段教材或错题，让模型切成问答卡片直接进库
2. **周报** —— 把这一周的复习日志汇总成一段话
3. **单 Agent + Skill 编排** —— 上面几个做成 skill，一个入口按意图分发

用户还没配 API key（`.env` 里的 `DEEPSEEK_API_KEY` 是空的），
所以真接口还没跑通过一次 —— 第一次跑通时记得确认一下响应解析。
详见 `README.md` 的「下一步路线」。

## 讲法偏好

- **不要一上来堆代码**，先讲流程和"为什么"，再上实现。
- 喜欢**先看成品**、边做边学，纯理论效果不好。
- 英文术语后面加中文解释。
- 回答尽量说清"为什么这么设计"，而不只是"这么写能跑"。
