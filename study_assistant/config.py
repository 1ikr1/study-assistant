"""配置：读 .env，拿 API key。

为什么自己写而不装 python-dotenv：这个项目的硬约定是**零第三方依赖**。
而 .env 的格式简单到二十行就够 —— 顺便也就知道那些库到底替你做了什么。

优先级：**真正的环境变量 > .env 文件**。
这是惯例，也是为了让"临时换个 key 试一下"不用去改文件。
"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根目录下的 .env
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# DeepSeek 的默认值。换成别的 OpenAI 兼容接口只要改 base_url。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def parse_env(text: str) -> dict[str, str]:
    """把 .env 的文本解析成字典。纯函数，方便逐条测。

    支持这几种写法（都是常见约定）：

        KEY=value
        KEY = value          # 等号两边有空格
        KEY="value"          # 带引号
        export KEY=value     # 从 shell 脚本里抄来的
        # 这是注释
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()

        # 去掉成对的引号（只有两边都是同一种引号才去，避免误伤 "abc）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key:
            out[key] = value
    return out


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """读 .env。文件不存在就返回空字典 —— 没有 key 是正常状态，不该报错。"""
    target = path or DEFAULT_ENV_PATH
    try:
        return parse_env(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def get_setting(name: str, default: str = "", path: Path | None = None) -> str:
    """取一项配置：先看环境变量，再看 .env，最后用默认值。"""
    if name in os.environ and os.environ[name].strip():
        return os.environ[name].strip()
    return read_env_file(path).get(name, default)


def llm_settings(path: Path | None = None) -> dict[str, str]:
    """大模型相关的全部配置。"""
    return {
        "api_key": get_setting("DEEPSEEK_API_KEY", "", path)
                   or get_setting("OPENAI_API_KEY", "", path),
        "base_url": get_setting("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL, path),
        "model": get_setting("DEEPSEEK_MODEL", DEFAULT_MODEL, path),
    }


def llm_configured(path: Path | None = None) -> bool:
    """配好 key 了吗。界面靠它决定要不要显示 AI 按钮。"""
    return bool(llm_settings(path)["api_key"])


def write_env_template(path: Path | None = None) -> Path:
    """如果还没有 .env，生成一个带注释的模板，用户只要填 key。

    为什么不直接把 key 写进代码：key 一旦进了代码，早晚会被提交到仓库。
    """
    target = path or DEFAULT_ENV_PATH
    if not target.exists():
        target.write_text(
            "# 个人学习助手 的配置。这个文件不会被提交（.gitignore 里排除了）。\n"
            "#\n"
            "# 去 https://platform.deepseek.com 申请一个 key，粘在等号后面。\n"
            "# 留空也能用 —— 只是「让 AI 讲一遍」那个功能会提示你先配置。\n"
            "\n"
            "DEEPSEEK_API_KEY=\n"
            f"DEEPSEEK_BASE_URL={DEFAULT_BASE_URL}\n"
            f"DEEPSEEK_MODEL={DEFAULT_MODEL}\n",
            encoding="utf-8",
        )
    return target
