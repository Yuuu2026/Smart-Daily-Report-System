"""在发送前编辑日报草稿：报个编号就改，顺带支持新增、删除、改投入工时。

草稿在 report 字典里是「- 一行一条」的字符串。这里拆成列表来编辑，改完再拼回去，
保证写进 published_items 的正文和网页发布完全一致。

只改真正会发出去的内容：四段正文 + 投入工时。总述和标签是终端里的提示信息，
不参与发送，所以不在编辑范围里，免得改了以为生效。

交互只有两层：问「改哪一条」→ 问「改成什么」。不再先让用户选 m，再报编号。
"""

SECTIONS = (
    ("finished", "今日完成"),
    ("solved", "解决的问题"),
    ("problems", "问题与风险"),
    ("next_steps", "下一步"),
)

SECTION_LABELS = {key: label for key, label in SECTIONS}
SECTION_BY_NUMBER = {str(index + 1): key for index, (key, _) in enumerate(SECTIONS)}
MAX_HOURS = 24
ADD_KEYWORDS = {"a", "add", "加", "添加"}
REMOVE_KEYWORDS = {"d", "delete", "remove", "删", "删除"}
HOURS_KEYWORDS = {"h", "hour", "hours", "工时"}
PREVIEW_KEYWORDS = {"p", "preview", "看", "预览"}
DONE_KEYWORDS = {"", "q", "quit", "完成", "返回", "ok"}


def split_items(text):
    """把「- a\\n- b」拆成 ['a', 'b']，兼容旧数据里的普通换行。"""
    items = []
    for line in (text or "").splitlines():
        item = line.strip()
        if item.startswith("-"):
            item = item[1:].strip()
        if item:
            items.append(item)
    return items


def join_items(items):
    return "\n".join(f"- {item}" for item in items if item)


def collect_items(report):
    """按段落顺序铺平成 [(段落键, 文本)]，编号就是列表下标 + 1。"""
    return [
        (key, item) for key, _ in SECTIONS for item in split_items(report.get(key))
    ]


def apply_items(report, items):
    """把铺平后的条目按段落分组写回 report，段落顺序固定。"""
    grouped = {key: [] for key, _ in SECTIONS}
    for key, item in items:
        grouped.setdefault(key, []).append(item)
    for key, _ in SECTIONS:
        report[key] = join_items(grouped.get(key, []))
    return report


def format_items(report):
    """带全局编号的清单：按段落分组，编号跨段连续。"""
    lines = []
    number = 0
    for key, label in SECTIONS:
        items = split_items(report.get(key))
        if not items:
            continue
        lines.append(f"  {label}")
        for item in items:
            number += 1
            lines.append(f"    {number}. {item}")
    if not number:
        lines.append("  （四段都还是空的）")
    return "\n".join(lines)


def format_numbered(report):
    """一条一行、前面标段落，改的时候直接报编号。"""
    lines = []
    for number, (key, item) in enumerate(collect_items(report), start=1):
        lines.append(f"    {number}. [{SECTION_LABELS[key]}] {item}")
    if not lines:
        lines.append("    （还没有任何条目，用 a 新增一条）")
    return "\n".join(lines)


def parse_hours(text):
    """返回规范化后的工时字符串；空串表示不填。填错时抛 ValueError。"""
    value = (text or "").strip()
    for suffix in ("小时", "hours", "hour", "h", "H"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
            break
    if not value:
        return ""
    try:
        number = float(value)
    except ValueError:
        raise ValueError("工时要填数字，例如 6.5") from None
    if number <= 0 or number > MAX_HOURS:
        raise ValueError(f"工时要在 0 到 {MAX_HOURS} 小时之间")
    return f"{number:g}"


def _ask_section(reader, say):
    say("  加到哪一段？")
    for index, (_, label) in enumerate(SECTIONS, start=1):
        say(f"    {index}. {label}")
    raw = reader("  编号 [1]：").strip() or "1"
    key = SECTION_BY_NUMBER.get(raw)
    if key is None:
        say("  没有这个编号，已跳过。")
    return key


def _ask_index(items, reader, say):
    raw = reader("  输入编号：").strip()
    if not raw.isdigit():
        say("  需要输入编号，已跳过。")
        return None
    index = int(raw) - 1
    if index < 0 or index >= len(items):
        say("  没有这个编号，已跳过。")
        return None
    return index


def _add(report, items, reader, say):
    key = _ask_section(reader, say)
    if key is None:
        return False
    text = reader("  内容：").strip()
    if not text:
        say("  没有输入内容，已跳过。")
        return False
    items.append((key, text))
    apply_items(report, items)
    say(f"  已添加到「{SECTION_LABELS[key]}」：{text}")
    return True


def _remove(report, items, reader, say):
    index = _ask_index(items, reader, say)
    if index is None:
        return False
    key, text = items.pop(index)
    apply_items(report, items)
    say(f"  已从「{SECTION_LABELS[key]}」删除：{text}")
    return True


def _edit_hours(report, reader, say):
    current = report.get("hours") or ""
    say(f"  当前工时：{current or '（未填）'}")
    raw = reader("  新的工时（小时，回车保留，输入 - 清空）：").strip()
    if not raw:
        return False
    if raw == "-":
        report["hours"] = ""
        say("  已清空工时。")
        return True
    try:
        report["hours"] = parse_hours(raw)
    except ValueError as error:
        say(f"  {error}")
        return False
    say(f"  工时已改成 {report['hours']} 小时。")
    return True


def _replace(report, items, raw, reader, say):
    """按编号改写一条：先确认改哪里，再问改成什么。"""
    index = int(raw) - 1
    if index < 0 or index >= len(items):
        say("  没有这个编号，已跳过。")
        return False
    key, current = items[index]
    say(f"  当前（{SECTION_LABELS[key]}）：{current}")
    text = reader("  改成（回车保留原文）：").strip()
    if not text or text == current:
        say("  没有变化。")
        return False
    items[index] = (key, text)
    apply_items(report, items)
    say(f"  已更新「{SECTION_LABELS[key]}」：{text}")
    return True


def edit_one(report, reader=None, writer=None):
    """改一处就返回：问「改哪一条」→ 问「改成什么」。

    直接输入编号就是修改；也可以输入 a 新增、d 删除、h 改工时、p 看全文。
    reader / writer 可注入，方便测试；默认用 input / print。
    返回 True 表示草稿确实变了。
    """
    reader = reader or input
    writer = writer or print

    def say(text=""):
        writer(text)

    items = collect_items(report)
    say()
    say("  改哪一条？直接输编号就是修改")
    say(format_numbered(report))
    say("  a 新增 · d 删除 · h 改工时 · p 看全文 · 回车返回")
    raw = reader("  > ").strip()
    lowered = raw.lower()

    if lowered in DONE_KEYWORDS:
        return False
    if lowered in ADD_KEYWORDS:
        return _add(report, items, reader, say)
    if lowered in REMOVE_KEYWORDS:
        return _remove(report, items, reader, say)
    if lowered in HOURS_KEYWORDS:
        return _edit_hours(report, reader, say)
    if lowered in PREVIEW_KEYWORDS:
        say()
        say(format_items(report))
        return False
    if not raw.isdigit():
        say("  输入编号（例如 2）才能改，或 a / d / h / p，回车返回。")
        return False
    return _replace(report, items, raw, reader, say)
