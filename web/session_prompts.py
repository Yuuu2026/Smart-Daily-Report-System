"""从本地 AI 会话日志里提取当天的用户提示词。

只读用户自己输入的话，不读模型回复和工具输出。支持两种日志：

- WorkBuddy：``~/.workbuddy/projects/<slug>/<session>.jsonl``
  ``{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "…"}], "timestamp": 毫秒}``
- Claude Code：``~/.claude/projects/<slug>/<session>.jsonl``
  ``{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "…"}]},
  "timestamp": "ISO", "cwd": "…"}``

提取结果用于生成日报正文，所以每条都保留来源与时间，方便回溯。
"""

import datetime
import json
import re
from pathlib import Path

DEFAULT_LIMIT = 40
MAX_TEXT = 600
MIN_TEXT = 4

# 会话外壳里的话，不是用户的需求。
NOISE_PATTERNS = (
    "继续",
    "继续吧",
    "好的",
    "好",
    "可以",
    "嗯",
    "谢谢",
    "ok",
    "okay",
    "go on",
    "continue",
    "do it",
    "yes",
    "continue from where you left off",
)

# 这些是会话里的自动续接，不是用户想做的事。
NOISE_PREFIXES = (
    "continue from where you left off",
    "continue from where you stopped",
)

# 粘贴进来的终端输出或日志，不能当作当天做了什么。
LOG_HINTS = (
    "traceback (most recent call last)",
    "exit code",
    "drwx",
    "total ",
    "@@ -",
    "stdout:",
    "stderr:",
    "stack trace",
)

COMMAND_PREFIXES = ("$ ", "> ", "ps c:", "cd ", "git ", "python ", "py ", "npm ", "pip ")

# 会话被压缩后，系统会把历史摘要当成一条 user 消息塞回来，那不是用户说的话。
SHELL_MARKERS = (
    "conversation_history_summary",
    "previous_user_message",
    "previous_tool_call",
)

SYSTEM_BLOCK = re.compile(r"<system-reminder[\s\S]*?</system-reminder>", re.IGNORECASE)
QUERY_BLOCK = re.compile(r"<user_query>([\s\S]*?)</user_query>", re.IGNORECASE)
FENCED_CODE = re.compile(r"```[\s\S]*?```")
TAG = re.compile(r"</?[a-z_][a-z0-9_-]*(?:\s[^<>]*)?/?>", re.IGNORECASE)
WHITESPACE = re.compile(r"[ \t\u3000]+")
BLANK_LINES = re.compile(r"\n{2,}")


def _normalize_slug(text):
    """把任意路径压成只剩字母数字与连字符，用于猜日志目录名。"""
    return re.sub(r"[^A-Za-z0-9]", "-", text)


def workbuddy_slug(root):
    """WorkBuddy 的目录名：盘符小写，其余分隔符统一成 -，中文原样保留。"""
    resolved = Path(root).resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = str(resolved)[len(resolved.drive):].lstrip("\\/")
    rest = "/".join(part for part in rest.split("\\") if part)
    rest = "/".join(part for part in rest.split("/") if part)
    return f"{drive}-{rest}".replace("/", "-")


def claude_slug(root):
    """Claude Code 的目录名：盘符小写，所有非字母数字字符（含中文）都变成 -。"""
    resolved = Path(root).resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = str(resolved)[len(resolved.drive):]
    return f"{drive}-{_normalize_slug(rest)}"


def session_dirs(root, home=None):
    """返回 [(来源, 目录)]，只包含真实存在的目录。"""
    home = Path(home) if home else Path.home()
    candidates = [
        ("workbuddy", home / ".workbuddy" / "projects" / workbuddy_slug(root)),
        ("claude", home / ".claude" / "projects" / claude_slug(root)),
    ]
    return [(name, path) for name, path in candidates if path.is_dir()]


def _parse_time(value):
    """两种日志分别是毫秒整数与 ISO 字符串，统一成本地时间。"""
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc).astimezone()
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone()
    return None


def _text_parts(record):
    """取出用户输入的纯文本片段，忽略 tool_result 之类的回灌内容。"""
    if record.get("type") == "message" and record.get("role") == "user":
        content = record.get("content")
        if isinstance(content, str):
            return [content]
        parts = []
        for part in content or []:
            if isinstance(part, dict) and part.get("type") in ("input_text", "text"):
                parts.append(part.get("text", ""))
        return parts
    if record.get("type") == "user":
        message = record.get("message") or {}
        if message.get("role") not in (None, "user"):
            return []
        content = message.get("content")
        if isinstance(content, str):
            return [content]
        parts = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
        return parts
    return []


def clean_text(raw):
    """去掉会话外壳，只留下用户真正想说的话。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = SYSTEM_BLOCK.sub(" ", text)
    queries = QUERY_BLOCK.findall(text)
    if queries:
        text = "\n".join(queries)
    else:
        text = FENCED_CODE.sub(" ", text)
        text = TAG.sub(" ", text)
    text = WHITESPACE.sub(" ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _is_noise(text):
    lowered = text.lower()
    compact = re.sub(r"\s+", "", lowered)
    if len(compact) < MIN_TEXT:
        return True
    if compact in NOISE_PATTERNS:
        return True
    if any(hint in lowered for hint in LOG_HINTS):
        return True
    if lowered.startswith(COMMAND_PREFIXES):
        return True
    if lowered.startswith(NOISE_PREFIXES):
        return True
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", text):
        return True
    if len(text) > MAX_TEXT:
        # 粘贴进来的整段文档，不是一句需求。
        return True
    return False


def _dedupe_key(text):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text.lower())[:40]


def _file_may_cover_day(path, day):
    """文件最后修改时间早于当天零点，说明不会有当天内容，直接跳过。"""
    try:
        modified = datetime.datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return False
    return modified >= day


def _iter_records(path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                if '"user"' not in line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def collect_prompts(root, day=None, home=None, sources=None, limit=DEFAULT_LIMIT):
    """收集指定日期（默认今天）的用户提示词，按时间升序返回。"""
    day = day or datetime.date.today()
    wanted = set(sources) if sources else None
    rows = []
    seen = set()

    for source, directory in session_dirs(root, home=home):
        if wanted and source not in wanted:
            continue
        for path in sorted(directory.glob("*.jsonl"), key=lambda item: item.stat().st_mtime):
            if not _file_may_cover_day(path, day):
                continue
            for record in _iter_records(path):
                moment = _parse_time(record.get("timestamp"))
                if not moment or moment.date() != day:
                    continue
                for part in _text_parts(record):
                    raw = part or ""
                    lowered = raw.lower()
                    if any(marker in lowered for marker in SHELL_MARKERS):
                        continue
                    text = clean_text(raw)
                    if not text or _is_noise(text):
                        continue
                    key = _dedupe_key(text)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "source": source,
                            "time": moment.strftime("%Y-%m-%d %H:%M"),
                            "date": moment.date().isoformat(),
                            "text": text[:MAX_TEXT],
                        }
                    )

    rows.sort(key=lambda item: item["time"])
    return rows[-limit:]


def source_summary(prompts):
    """按来源统计条数，供命令行显示。"""
    counts = {}
    for item in prompts:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    return counts
