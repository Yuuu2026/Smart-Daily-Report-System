"""检查成员日报内容的粒度，输出可操作的中文提示。"""

SECTION_LABELS = {
    "finished": "完成事项",
    "problems": "问题与风险",
    "next_steps": "下一步计划",
}


def _visible_length(text):
    return len("".join((text or "").split()))


def inspect_progress_text(section, text):
    """检查一个发布段落的粒度；空段落交由整体发布校验处理。"""
    text = (text or "").strip()
    if not text:
        return None

    length = _visible_length(text)
    if length < 8:
        return (
            f"“{section}”内容偏粗，建议写成“工作对象 + 完成内容 + 状态或证据”，"
            "约 15-50 字。"
        )
    if length > 80:
        return f"“{section}”内容偏细，建议只保留一个主要结果，细节放入证据链接或附录。"

    separators = sum(text.count(mark) for mark in ("，", "；", "。"))
    if length > 60 and separators >= 2:
        return (
            f"“{section}”内容偏细且包含多个主题，"
            "建议拆成一次发布或保留一个主要结果。"
        )
    return None


def quality_warnings(sections):
    """返回发布前的粒度提示；提示不阻止发布。"""
    warnings = []
    for field, label in SECTION_LABELS.items():
        warning = inspect_progress_text(label, sections.get(field, ""))
        if warning:
            warnings.append(warning)
    return warnings
