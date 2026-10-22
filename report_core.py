"""日报生成器共享核心。

V1 命令行程序和 V2 本机 Web 程序共用数据校验、分组与 Markdown 渲染逻辑。
"""

import datetime
import re

# 三类记录及其必填字段（含公共字段 member）
RECORD_FIELDS = {
    "commits": ["member", "repo", "message"],
    "tasks": ["member", "task", "progress"],
    "collaborations": ["member", "channel", "summary"],
}

# 类别的中文名
CATEGORY_NAMES = {
    "commits": "代码提交",
    "tasks": "任务进展",
    "collaborations": "协作沟通",
}

# 成员小节内三类的固定显示顺序
CATEGORY_ORDER = ("commits", "tasks", "collaborations")

# 成员发布内容中的新旧段落标签。旧标签用于兼容历史数据库记录。
PUBLISHED_SECTION_LABELS = {
    "status": ("工作状态",),
    "solved": ("解决的问题", "解决了什么问题"),
    "finished": ("完成事项", "今天完成了什么"),
    "problems": ("问题与风险", "遇到了什么问题"),
    "next_steps": ("下一步计划", "下一步准备做什么"),
    "code_changes": ("最近代码改动",),
}
PUBLISHED_SECTION_HEADER_RE = re.compile(
    r"^\s*("
    + "|".join(
        label
        for labels in PUBLISHED_SECTION_LABELS.values()
        for label in labels
    )
    + r")\s*[：:]\s*$"
)
PUBLISHED_SECTION_INLINE_RE = re.compile(
    r"^\s*("
    + "|".join(
        label
        for labels in PUBLISHED_SECTION_LABELS.values()
        for label in labels
    )
    + r")\s*[：:]\s*(.+?)\s*$"
)


def type_name(value):
    """返回一个值的中文类型名，用于错误提示。"""
    if isinstance(value, dict):
        return "对象"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, str):
        return "字符串"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, (int, float)):
        return "数字"
    return type(value).__name__


def validate(data):
    """按 data-format.md 校验数据，返回错误信息列表（为空表示全部合法）。"""
    errors = []

    if not isinstance(data, dict):
        return ["结构错误：JSON 顶层应为对象（object）"]

    for category, fields in RECORD_FIELDS.items():
        if category not in data:
            continue  # 该键缺省，表示该类无记录，合法

        items = data[category]
        if not isinstance(items, list):
            errors.append(
                f"结构错误：{CATEGORY_NAMES[category]}（{category}）"
                f"应为数组，实际是 {type_name(items)}"
            )
            continue

        for i, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                errors.append(
                    f"{CATEGORY_NAMES[category]}（{category}）第 {i} 条："
                    f"应为对象，实际是 {type_name(item)}"
                )
                continue

            member = item.get("member")
            if isinstance(member, str) and member.strip() != "":
                loc = f"成员「{member.strip()}」"
            else:
                loc = "成员缺失"

            for field in fields:
                value = item.get(field)
                if not (isinstance(value, str) and value.strip() != ""):
                    errors.append(
                        f"{CATEGORY_NAMES[category]}（{category}）第 {i} 条 {loc}："
                        f"字段「{field}」缺失或不是非空字符串"
                    )

    return errors


def group_by_member(data):
    """把三类记录按成员合并，成员名升序返回。"""
    groups = {}
    for category in CATEGORY_ORDER:
        for item in data.get(category, []):
            member = item["member"].strip()
            if member not in groups:
                groups[member] = {"commits": [], "tasks": [], "collaborations": []}
            groups[member][category].append(item)
    return dict(sorted(groups.items()))


def parse_progress_sections(content):
    """解析成员发布内容，兼容新旧两套段落标签。"""
    result = {field: "" for field in PUBLISHED_SECTION_LABELS}
    field_by_label = {
        label: field
        for field, labels in PUBLISHED_SECTION_LABELS.items()
        for label in labels
    }
    current_field = None
    for line in (content or "").splitlines():
        match = PUBLISHED_SECTION_HEADER_RE.match(line)
        if match:
            current_field = field_by_label[match.group(1)]
            continue
        inline_match = PUBLISHED_SECTION_INLINE_RE.match(line)
        if inline_match:
            current_field = field_by_label[inline_match.group(1)]
            result[current_field] = inline_match.group(2)
            continue
        if current_field and line.strip():
            result[current_field] = "\n".join(
                part for part in (result[current_field], line.strip()) if part
            )

    if not any(result.values()) and (content or "").strip():
        # 兼容更早期未分段的成员发布内容。
        result["finished"] = content.strip()
    return result


def compact_progress_text(text, max_length=80):
    """把正文压成一条可读结果；证据和长文本由附录承载。"""
    text = " ".join((text or "").split())
    if len(text) <= max_length:
        return text
    truncated = text[:max_length - 2]
    boundary = max(
        truncated.rfind("，"),
        truncated.rfind("。"),
        truncated.rfind("；"),
        truncated.rfind(" "),
    )
    if boundary >= int(max_length * 0.55):
        truncated = truncated[:boundary]
    return truncated.rstrip("，。；,.;：: ") + "……"


def _value(item, key, default=""):
    """从 dict 或 sqlite3.Row 安全取值。"""
    try:
        return item[key] if item[key] is not None else default
    except (KeyError, IndexError, TypeError):
        return default


def _first_line(text, empty_text="（无说明）"):
    text = (text or "").strip()
    return text.splitlines()[0].strip() if text else empty_text


def _format_generated_at(generated_at):
    if generated_at is None:
        generated_at = datetime.datetime.now().astimezone()
    if isinstance(generated_at, datetime.datetime):
        return generated_at.strftime("%Y-%m-%d %H:%M")
    return str(generated_at)


def render_structured_report(
    groups,
    date,
    published_rows=(),
    github_rows=(),
    generated_at=None,
    empty_text="今日无记录",
):
    """渲染固定结构的团队日报，明确来源、粒度和证据边界。"""
    published_rows = list(published_rows or [])
    github_rows = list(github_rows or [])
    task_count = sum(len(items.get("tasks", [])) for items in groups.values())
    collaboration_count = sum(
        len(items.get("collaborations", [])) for items in groups.values()
    )
    members = set(groups)
    members.update(_value(row, "member") for row in published_rows)
    members.update(_value(row, "member") for row in github_rows)
    members.discard("")

    lines = [
        f"# 团队智能日报 · {date}",
        "",
        f"> 系统自动生成 · 生成时间：{_format_generated_at(generated_at)}",
        f"> 数据窗口：{date} 00:00 - 23:59（北京时间）",
        (
            "> 数据来源：GitHub 提交 "
            f"{len(github_rows)} 条、成员发布 {len(published_rows)} 条、"
            f"任务记录 {task_count} 条、协作记录 {collaboration_count} 条"
        ),
        "",
        "## 今日概览",
        (
            f"- [系统汇总] 归档 GitHub 提交 {len(github_rows)} 条、"
            f"成员发布 {len(published_rows)} 条、协作记录 {collaboration_count} 条。"
        ),
        (
            f"- [系统汇总] 今日参与成员 {len(members)} 人，代码变更 "
            f"+{sum(int(_value(row, 'additions', 0) or 0) for row in github_rows)} / "
            f"-{sum(int(_value(row, 'deletions', 0) or 0) for row in github_rows)} 行。"
        ),
        "",
        "## 成员进展",
    ]

    published_by_member = {}
    for row in published_rows:
        published_by_member.setdefault(_value(row, "member"), []).append(row)
    github_by_member = {}
    for row in github_rows:
        github_by_member.setdefault(_value(row, "member"), []).append(row)

    for member in sorted(members):
        lines.extend(["", f"### {member}", ""])
        member_groups = groups.get(member, {})

        completed_items = []
        for row in github_by_member.get(member, []):
            repository = _value(row, "repository", "未记录仓库")
            message = _first_line(_value(row, "message"))
            additions = int(_value(row, "additions", 0) or 0)
            deletions = int(_value(row, "deletions", 0) or 0)
            changed_files = int(_value(row, "changed_files", 0) or 0)
            completed_items.append(
                "[GitHub] "
                + compact_progress_text(
                    f"{repository}：{message}"
                    f"（+{additions} / -{deletions}，{changed_files} 个文件）"
                )
            )
        for row in published_by_member.get(member, []):
            sections = parse_progress_sections(_value(row, "content"))
            if sections["finished"]:
                completed_items.append(
                    f"[成员发布] {sections['status'] or '未标记状态'}："
                    f"{compact_progress_text(sections['finished'])}"
                )
        for item in member_groups.get("commits", []):
            legacy_commit = (
                f"{item.get('repo', '未记录仓库')}：{item.get('message', '')}"
            )
            completed_items.append(
                f"[系统汇总] {compact_progress_text(legacy_commit)}"
            )
        for item in member_groups.get("tasks", []):
            completed_items.append(
                f"[系统汇总] {item.get('task', '未记录任务')}："
                f"{compact_progress_text(item.get('progress', ''))}"
            )
        for item in member_groups.get("collaborations", []):
            completed_items.append(
                f"[协作记录] {item.get('channel', '未记录渠道')}："
                f"{compact_progress_text(item.get('summary', ''))}"
            )

        problem_items = []
        next_step_items = []
        for row in published_by_member.get(member, []):
            sections = parse_progress_sections(_value(row, "content"))
            if sections["problems"]:
                problem_items.append(
                    f"[成员发布] {compact_progress_text(sections['problems'])}"
                )
            if sections["next_steps"]:
                next_step_items.append(
                    f"[成员发布] {compact_progress_text(sections['next_steps'])}"
                )

        for heading, items in (
            ("完成事项", completed_items),
            ("问题与风险", problem_items),
            ("下一步计划", next_step_items),
        ):
            lines.append(f"#### {heading}")
            if items:
                lines.extend(f"- {item}" for item in items)
            else:
                lines.append(f"- [系统汇总] {empty_text}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_report(groups, date, empty_text="今日无记录"):
    """把分组结果渲染成 Markdown 日报字符串。

    empty_text 用于没有记录的类别；默认文案保持 V1 输出不变。
    """
    lines = [f"# 团队日报 · {date}", ""]

    if not groups:
        for category in CATEGORY_ORDER:
            lines.append(f"## {CATEGORY_NAMES[category]}")
            lines.append(empty_text)
            lines.append("")
        return "\n".join(lines)

    for member in groups:
        lines.append(f"## {member}")
        lines.append("")

        for category in CATEGORY_ORDER:
            items = groups[member][category]
            lines.append(f"### {CATEGORY_NAMES[category]}")
            if items:
                for item in items:
                    if category == "commits":
                        lines.append(f"- {item['repo']}：{item['message']}")
                    elif category == "tasks":
                        lines.append(f"- {item['task']}：{item['progress']}")
                    else:  # collaborations
                        lines.append(f"- {item['channel']}：{item['summary']}")
            else:
                lines.append(empty_text)
            lines.append("")

    return "\n".join(lines)
