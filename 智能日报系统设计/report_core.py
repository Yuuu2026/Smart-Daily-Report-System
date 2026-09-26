"""日报生成器共享核心。

V1 命令行程序和 V2 本机 Web 程序共用数据校验、分组与 Markdown 渲染逻辑。
"""

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
