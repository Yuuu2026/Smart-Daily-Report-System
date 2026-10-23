"""把一则日报整理成邮件用的结构化简报。

设计原则：概览里的每个数字都要能追溯到真实数据，宁可少显示一格也不编造。

- 完成 / 进行中：从成员填写的正文拆句得到，两者相加就是「今日任务」总数
- +新增 / −删除：来自最近的代码改动统计
- 投入工时：成员主动填写，没填就不显示这一格
- 改动分布 / 工作构成：代码改动按模块汇总，或按近 7 天数据来源汇总

系统里没有的数据（例如「项目完成 85%」这种需要人工维护的进度），这里不做估算，
由调用方决定是否渲染，避免日报里出现查无实据的百分比。
"""

from __future__ import annotations

import re

MAX_ITEM_LENGTH = 42

LINE_PREFIX = re.compile(r"^\s*(?:[-*•·—]|\(?\d{1,2}[.)、]|[①-⑳])\s*")
SENTENCE_SPLIT = re.compile(r"[。！？!?]")
CLAUSE_SPLIT = re.compile(r"[；;]")

# 标签按优先级排序：越靠前越能代表这条日报的主要工作。
TAG_RULES = (
    ("#前端优化", ("样式", "页面", "布局", "模板", "css", "html", "界面", "移动端", "响应式")),
    ("#自动化", ("自动", "定时", "脚本", "批处理", "守护", "巡检")),
    ("#日报系统", ("日报", "归档", "工作台")),
    ("#邮件通知", ("邮件", "smtp", "发信", "收件", "通知")),
    ("#飞书集成", ("飞书", "群聊", "机器人", "webhook")),
    ("#GitHub", ("github", "仓库", "提交", "commit", "分支")),
    ("#数据统计", ("统计", "数据", "图表", "指标", "报表", "柱状")),
    ("#问题修复", ("修复", "报错", "异常", "排查", "定位", "bug", "失败")),
    ("#测试", ("测试", "用例", "回归", "断言", "unittest")),
    ("#部署上线", ("部署", "上线", "runner", "ci", "发布到")),
    ("#文档", ("文档", "说明", "readme", "注释")),
)

WORKLOAD_LABELS = {
    "reports": "日报归档",
    "tasks": "任务记录",
    "collaborations": "协作记录",
    "commits": "代码提交",
    "messages": "群聊消息",
}

INSIGHT_TONES = {
    "good": "状态良好",
    "steady": "状态平稳",
    "tight": "节奏偏紧",
    "low": "记录偏少",
}


def trim(text, limit=MAX_ITEM_LENGTH):
    """把过长的一行截断，保留可读性。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ，,、") + "…"


def split_items(text, limit=8):
    """把一段自由文字拆成条目：先去行首符号，再按分号与句号断开。"""
    items = []
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        line = LINE_PREFIX.sub("", raw_line).strip()
        if not line:
            continue
        pieces = []
        for clause in CLAUSE_SPLIT.split(line):
            clause = clause.strip()
            if not clause:
                continue
            # 短句不拆，长句才按句号断，避免把一句话切成碎片。
            if len(clause) > 26:
                pieces.extend(
                    part.strip() for part in SENTENCE_SPLIT.split(clause) if part.strip()
                )
            else:
                pieces.append(clause)
        for piece in pieces:
            piece = trim(piece.strip(" ，,、"))
            if len(piece) >= 2 and piece not in items:
                items.append(piece)
            if len(items) >= limit:
                return items
    return items


def extract_tags(*texts, limit=4):
    """从正文里挑出最能概括本次工作的标签，匹配不到就返回空列表。"""
    blob = " ".join(str(text or "") for text in texts).lower()
    if not blob.strip():
        return []
    tags = []
    for label, keywords in TAG_RULES:
        if any(keyword in blob for keyword in keywords):
            tags.append(label)
        if len(tags) >= limit:
            break
    return tags


def module_label(path):
    """把文件路径归到一个好读的模块名，用于改动分布。"""
    path = str(path or "").replace("\\", "/").strip().lower()
    if not path:
        return "其他文件"
    if "templates" in path:
        return "页面模板"
    if path.endswith(".css"):
        return "页面样式"
    if path.endswith((".html", ".htm")):
        return "页面模板"
    if path.startswith("scripts/"):
        return "脚本工具"
    if path.endswith((".md", ".txt")):
        return "文档说明"
    if path.endswith(".py"):
        return "Python 逻辑"
    return path.rsplit("/", 1)[-1] or "其他文件"


def build_mix(recent_changes=None, workload=None, limit=4):
    """生成一块占比条形图的数据。

    优先按代码改动的模块分布（信息量最大），改动只涉及一个模块时，
    回退到近 7 天的数据来源构成；两者都凑不出两条，就返回空表示不渲染。
    """
    buckets = {}
    for item in recent_changes or []:
        additions = int(item.get("additions") or 0)
        if additions <= 0:
            continue
        label = module_label(item.get("path", ""))
        buckets[label] = buckets.get(label, 0) + additions

    if len(buckets) >= 2:
        rows = sorted(buckets.items(), key=lambda pair: -pair[1])
        head, rest = rows[:limit], rows[limit:]
        if rest:
            head.append(("其他模块", sum(value for _, value in rest)))
        total = sum(value for _, value in head) or 1
        return {
            "rows": [
                {"label": label, "value": value, "percent": round(value * 100 / total)}
                for label, value in head
            ],
            "caption": f"按新增行数占比 · 共 +{total} 行",
        }

    totals = {key: 0 for key in WORKLOAD_LABELS}
    for day in (workload or {}).get("days", []):
        for key in totals:
            totals[key] += int(day.get(key) or 0)
    active = sorted(
        ((key, value) for key, value in totals.items() if value > 0),
        key=lambda pair: -pair[1],
    )[:limit]
    if len(active) < 2:
        return {"rows": [], "caption": ""}
    total = sum(value for _, value in active) or 1
    return {
        "rows": [
            {
                "label": WORKLOAD_LABELS[key],
                "value": value,
                "percent": round(value * 100 / total),
            }
            for key, value in active
        ],
        "caption": f"近 7 天条目来源占比 · 共 {total} 条",
    }


def _coerce_hours(hours):
    """工时允许字符串或数字，非法值一律当作没填。"""
    if hours is None or hours == "":
        return None
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 24:
        return None
    return value


def _build_summary(done_items, plan_items, additions, deletions, problems):
    """用一句半的话概括今天，事实来自已拆分好的条目。"""
    sentences = []
    if done_items:
        lead = done_items[0]
        if len(done_items) > 1:
            sentences.append(f"今天主要完成「{lead}」等 {len(done_items)} 项工作。")
        else:
            sentences.append(f"今天主要完成「{lead}」。")
    elif plan_items:
        sentences.append("今天还没有标记完成的事项，先按计划推进。")

    tail = []
    if additions or deletions:
        tail.append(f"代码净增 {additions - deletions:+d} 行")
    if plan_items:
        tail.append(f"{len(plan_items)} 项安排在明天")
    if problems:
        tail.append("留有 1 项风险待跟进")
    if tail:
        sentences.append("；".join(tail) + "。")
    return "".join(sentences)


def _build_insight(done_items, plan_items, problems):
    """根据完成量、风险与计划给一句状态和一句建议。"""
    risk = bool(problems)
    if len(done_items) >= 3 and not risk:
        tone = "good"
    elif len(done_items) >= 2:
        tone = "steady"
    elif risk:
        tone = "tight"
    else:
        tone = "low"

    if risk:
        first_problem = split_items(problems, 1)
        focus = first_problem[0] if first_problem else problems
        advice = f"先处理「{trim(focus, 24)}」，再推进新任务。"
    elif plan_items:
        advice = f"明天优先推进「{trim(plan_items[0], 24)}」，做完再开下一项。"
    else:
        advice = "补一条明日计划，明天开工时可以直接接上。"

    return {"state": INSIGHT_TONES[tone], "tone": tone, "advice": advice}


def build_brief(
    solved="",
    finished="",
    problems="",
    next_steps="",
    recent_changes=None,
    workload=None,
    hours=None,
):
    """把日报的各段文字整理成邮件要用的完整简报。"""
    recent_changes = recent_changes or []
    done_items = split_items("\n".join(part for part in (solved, finished) if part), limit=8)
    plan_items = split_items(next_steps, limit=6)
    additions = sum(int(item.get("additions") or 0) for item in recent_changes)
    deletions = sum(int(item.get("deletions") or 0) for item in recent_changes)
    problems = (problems or "").strip()
    hours_value = _coerce_hours(hours)

    overview = {
        "tasks": len(done_items) + len(plan_items),
        "done": len(done_items),
        "doing": len(plan_items),
        "additions": additions,
        "deletions": deletions,
        "net": additions - deletions,
        "hours": hours_value,
        "hours_label": f"{hours_value:g}h" if hours_value else "",
    }

    mix = build_mix(recent_changes, workload)
    return {
        "overview": overview,
        "summary": _build_summary(done_items, plan_items, additions, deletions, problems),
        "tags": extract_tags(
            solved,
            finished,
            next_steps,
            problems,
            " ".join(
                str(item.get("summary", "")) + " " + str(item.get("path", ""))
                for item in recent_changes
            ),
        ),
        "done_items": done_items,
        "plan_items": plan_items,
        "problem_items": split_items(problems, limit=4),
        "mix": mix["rows"],
        "mix_caption": mix["caption"],
        "insight": _build_insight(done_items, plan_items, problems),
        "change_total": {"additions": additions, "deletions": deletions},
    }
