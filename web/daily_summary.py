"""把当天的提交、代码改动、AI 会话提示词与飞书消息整理成一份可发送的日报。

分工很明确，避免把「想做」说成「做完」：

- 完成项只来自 Git 提交（每条都能追到一次 commit）
- 本地未提交的改动单独标注，措辞用「调整」而不是「完成」
- 会话提示词用来生成总述、标签与下一步计划，不直接当成完成项
- 飞书消息原样带上，作为团队协作的上下文

所有字段都能回到某个数据源；取不到就留空，不用占位文字充数。
"""

import datetime
import json
import re
import subprocess
from pathlib import Path

from web.daily_brief import extract_tags, module_label, trim

MAX_ITEM = 60
MAX_PLAN = 4
MAX_PROBLEM = 3
ENTRY_LIMIT = 8

CONVENTIONAL = re.compile(
    r"^\s*(feat|fix|docs|refactor|chore|test|style|perf|build|ci|revert)"
    r"(\([^)]*\))?!?\s*[:：]\s*",
    re.IGNORECASE,
)
MERGE_SUBJECT = re.compile(r"^merge\b", re.IGNORECASE)
FIX_PREFIX = re.compile(r"^(修复|修正|解决|修好|改掉|纠正)")
FIX_CONVENTIONAL = re.compile(r"^\s*fix(\([^)]*\))?!?\s*[:：]", re.IGNORECASE)
# 「记忆：…」「记录…」这类提交是给 AI 留的上下文，不进日报。
INTERNAL_SUBJECT = re.compile(r"^\s*(记忆|记录|笔记)\s*[:：]?")
PLAN_HINT = re.compile(r"下一步|接下来|还需要|还需|待办|还没|明天|后续|之后|todo", re.IGNORECASE)
PROBLEM_HINT = re.compile(r"报错|失败|异常|不对|不生效|没反应|超时|卡住|崩溃|出错|有问题|不能", re.IGNORECASE)
PROBLEM_DONE = re.compile(r"已经|已修|修好|解决|搞定|完成")
SENTENCE_SPLIT = re.compile(r"[。！？；\n]+")


def _run_git(root, *args):
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None


def clean_subject(subject):
    """去掉 conventional commit 前缀与尾部标点，留下能直接念的一句。"""
    text = (subject or "").strip()
    text = CONVENTIONAL.sub("", text).strip()
    text = text.replace("\u3000", " ").strip()
    text = re.sub(r"\s*[.。]$", "", text)
    return text


def collect_commits(root, day=None):
    """读取指定日期（默认今天）的非合并提交，附带改动文件数与行数。"""
    day = day or datetime.date.today()
    start = f"{day.isoformat()} 00:00:00"
    end = f"{day.isoformat()} 23:59:59"
    result = _run_git(
        root,
        "log",
        "--no-merges",
        f"--since={start}",
        f"--until={end}",
        "--date=format:%Y-%m-%d %H:%M",
        "--pretty=format:__COMMIT__%H%x1f%ad%x1f%s%x1f%b",
        "--numstat",
    )
    if result is None or result.returncode != 0:
        return []

    commits = []
    for chunk in result.stdout.split("__COMMIT__"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        header, _, rest = chunk.partition("\n")
        columns = header.split("\x1f")
        if len(columns) < 3:
            continue
        sha, moment, subject = columns[0], columns[1], columns[2]
        body = columns[3] if len(columns) > 3 else ""
        if MERGE_SUBJECT.match(subject or ""):
            continue
        additions = deletions = files = 0
        paths = []
        for row in rest.splitlines():
            parts = row.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            paths.append(parts[2].strip())
            if parts[0].isdigit():
                additions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
        commits.append(
            {
                "sha": sha,
                "short": sha[:7],
                "time": moment.strip(),
                "subject": clean_subject(subject),
                "raw_subject": subject.strip(),
                "body": (body or "").strip(),
                "files": files,
                "additions": additions,
                "deletions": deletions,
                "paths": paths,
            }
        )
    commits.sort(key=lambda item: item["time"])
    return commits


def collect_changes(root, day=None):
    """读取本地代码改动缓存，只保留指定日期的条目。"""
    day = day or datetime.date.today()
    state = Path(root) / ".claude" / "recent_code_changes.json"
    try:
        items = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        stamped = str(item.get("time", ""))[:10]
        if stamped != day.isoformat():
            continue
        rows.append(item)
    return rows


def collect_feishu(connection, day=None):
    """读取当天已同步的飞书群聊消息。"""
    day = day or datetime.date.today()
    try:
        rows = connection.execute(
            """SELECT chat_name, member_name, summary
               FROM feishu_messages
               WHERE work_date = ?
               ORDER BY id""",
            (day.isoformat(),),
        ).fetchall()
    except Exception:
        return []
    items = []
    seen = set()
    for row in rows:
        summary = (row["summary"] or "").strip()
        if not summary:
            continue
        key = (row["member_name"], summary)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "chat_name": row["chat_name"] or "",
                "member": row["member_name"] or "成员",
                "summary": summary,
            }
        )
    return items


def collect_signals(root, connection=None, day=None, home=None):
    """一次拿齐当天的四类信号。"""
    day = day or datetime.date.today()
    from web.session_prompts import collect_prompts

    return {
        "day": day,
        "commits": collect_commits(root, day),
        "changes": collect_changes(root, day),
        "prompts": collect_prompts(root, day=day, home=home),
        "feishu": collect_feishu(connection, day) if connection is not None else [],
    }


def _sentences(texts):
    for text in texts:
        for line in SENTENCE_SPLIT.split(text):
            line = line.strip(" -·*#\t")
            if len(line) >= 6:
                yield line


def pick_plans(prompts, limit=MAX_PLAN):
    """从提示词里挑出明确写了「接下来 / 待办」的句子。"""
    picked = []
    seen = set()
    for sentence in _sentences(item["text"] for item in prompts):
        if not PLAN_HINT.search(sentence):
            continue
        cleaned = PLAN_HINT.sub("", sentence).strip("：:，, ")
        cleaned = trim(cleaned, MAX_ITEM)
        if len(cleaned) < 4 or cleaned in seen:
            continue
        seen.add(cleaned)
        picked.append(cleaned)
        if len(picked) >= limit:
            break
    return picked


def pick_problems(prompts, limit=MAX_PROBLEM):
    """从提示词里挑出还没被「已解决」措辞覆盖的问题描述。"""
    picked = []
    seen = set()
    for sentence in _sentences(item["text"] for item in prompts):
        if not PROBLEM_HINT.search(sentence):
            continue
        if PROBLEM_DONE.search(sentence) and not re.search(r"还没|仍然|依然", sentence):
            continue
        cleaned = trim(sentence, MAX_ITEM)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        picked.append(cleaned)
        if len(picked) >= limit:
            break
    return picked


def _change_line(change):
    path = str(change.get("path", ""))
    additions = int(change.get("additions") or 0)
    deletions = int(change.get("deletions") or 0)
    return f"{module_label(path)}：{Path(path).name}（未提交，+{additions} / −{deletions}）"


def build_report(signals, hours=""):
    """把信号整理成邮件要用的四段文字与统计。"""
    commits = signals.get("commits") or []
    changes = signals.get("changes") or []
    prompts = signals.get("prompts") or []
    feishu = signals.get("feishu") or []
    day = signals.get("day") or datetime.date.today()

    done_items = []
    solved_items = []
    seen = set()
    ranked = sorted(commits, key=_priority)
    for commit in ranked:
        raw = commit["raw_subject"]
        if INTERNAL_SUBJECT.match(raw):
            continue
        subject = commit["subject"] or raw
        if not subject:
            continue
        key = re.sub(r"\W", "", subject)[:24]
        if key in seen:
            continue
        seen.add(key)
        if FIX_CONVENTIONAL.match(raw) or FIX_PREFIX.match(subject):
            solved_items.append(subject)
        else:
            done_items.append(subject)
        if len(done_items) + len(solved_items) >= ENTRY_LIMIT:
            break

    uncommitted = []
    committed_paths = set()
    for commit in commits:
        committed_paths.update(commit.get("paths") or [])
    for change in changes:
        path = str(change.get("path", ""))
        if path in committed_paths:
            continue
        uncommitted.append(_change_line(change))

    plans = pick_plans(prompts)
    problems = pick_problems(prompts)

    additions = sum(int(item.get("additions") or 0) for item in commits)
    deletions = sum(int(item.get("deletions") or 0) for item in commits)
    files = sum(int(item.get("files") or 0) for item in commits)

    warnings = []
    if not commits and not changes:
        warnings.append("今天没有读到代码提交或本地改动，完成项为空。")
    elif not commits:
        warnings.append("今天的改动还没有提交，完成项按本地改动生成。")
    if not prompts:
        warnings.append("没有读到会话提示词，总述只依据提交生成。")

    topics = _topics(commits, changes, prompts)
    summary = _summary(commits, files, additions, deletions, topics, prompts, feishu)

    if not plans and feishu:
        # 群里提到的待办也可以作为下一步，但只在用户没有别处写计划时使用。
        plans = [
            trim(item["summary"], MAX_ITEM)
            for item in feishu
            if PLAN_HINT.search(item["summary"])
        ][:MAX_PLAN]

    solved = "\n".join(f"- {item}" for item in solved_items)
    if uncommitted and not commits:
        done_items.extend(uncommitted)
    finished = "\n".join(f"- {item}" for item in done_items)
    next_steps = "\n".join(f"- {item}" for item in plans)
    problems_text = "\n".join(f"- {item}" for item in problems)

    tags = extract_tags(
        "\n".join(done_items),
        "\n".join(solved_items),
        "\n".join(item["text"] for item in prompts),
        " ".join(str(item.get("path", "")) for item in changes),
        limit=4,
    )

    return {
        "date": day.isoformat(),
        "solved": solved,
        "finished": finished,
        "problems": problems_text,
        "next_steps": next_steps,
        "summary": summary,
        "topics": topics,
        "tags": tags,
        "hours": hours,
        "sources": {
            "commits": len(commits),
            "files": files,
            "additions": additions,
            "deletions": deletions,
            "prompts": len(prompts),
            "feishu": len(feishu),
            "uncommitted": len(uncommitted),
        },
        "warnings": warnings,
        "done_items": done_items,
        "solved_items": solved_items,
        "plan_items": plans,
        "problem_items": problems,
    }


def _priority(commit):
    """功能提交排前面，文档与测试靠后，内部记忆不参与。"""
    raw = commit["raw_subject"]
    if INTERNAL_SUBJECT.match(raw):
        return 3
    if re.match(r"^\s*(docs|test|chore)\b", raw, re.IGNORECASE):
        return 2
    return 1


def _topic_label(subject):
    """取冒号前的短语做主题，避免把整条提交塞进总述。"""
    label = re.split(r"[：:]", subject or "", maxsplit=1)[0].strip()
    return trim(label, 18)


def _topics(commits, changes, prompts):
    """总述里用来概括「今天在做什么」的短词，按来源可信度排序。"""
    topics = []
    for commit in reversed(commits):
        if INTERNAL_SUBJECT.match(commit["raw_subject"]):
            continue
        label = _topic_label(commit["subject"])
        if label and label not in topics:
            topics.append(label)
        if len(topics) >= 2:
            break
    if topics:
        return topics
    for change in changes:
        label = module_label(str(change.get("path", "")))
        if label and label not in topics:
            topics.append(label)
        if len(topics) >= 2:
            break
    return topics


def _summary(commits, files, additions, deletions, topics, prompts, feishu):
    """一句话概括当天，数字全部来自提交统计。"""
    if commits:
        head = f"今天提交 {len(commits)} 次"
        if files:
            head += f"，改动 {files} 个文件（+{additions} / −{deletions}）"
        if topics:
            head += "，主要集中在" + "、".join(topics)
        tail = "。"
        if prompts:
            tail += f"期间与 AI 协作 {len(prompts)} 轮。"
        if feishu:
            tail += f"同步了 {len(feishu)} 条群聊记录。"
        return head + tail
    if prompts:
        return (
            f"今天没有代码提交，与 AI 协作 {len(prompts)} 轮，"
            "主要在处理当天提出的需求。"
        )
    if feishu:
        return f"今天没有代码提交，群聊记录了 {len(feishu)} 条协作消息。"
    return "今天没有读到可用的工作记录。"


def stream_items(feishu, commits, limit=8):
    """给邮件「数据流水」用的条目，提交与群聊各自带标签。"""
    rows = []
    for commit in commits[-limit:]:
        rows.append(
            {
                "tag": "代码提交",
                "member": commit.get("short", ""),
                "summary": commit["subject"],
            }
        )
    for item in feishu[-limit:]:
        rows.append(
            {
                "tag": "飞书群聊",
                "member": item["member"],
                "summary": item["summary"],
            }
        )
    return rows[-limit:]
