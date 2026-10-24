#!/usr/bin/env python
"""在终端里生成并发送今日日报，不需要打开网页。

默认流程：读取当天的 Git 提交、本地代码改动、AI 会话提示词与飞书消息，
拼出一份日报草稿，打印在终端里；确认收件邮箱后写入归档并发送邮件。

    py -3.12 scripts/report_cli.py                 # 今天，交互确认
    py -3.12 scripts/report_cli.py --dry-run       # 只看草稿，不发
    py -3.12 scripts/report_cli.py --date 2026-10-22
    py -3.12 scripts/report_cli.py --to a@x.com,b@y.com --yes
    py -3.12 scripts/report_cli.py --install-hook  # 每次 git commit 后询问
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import email_notifications  # noqa: E402
from web.app import database_connection  # noqa: E402
from web.daily_summary import build_report, collect_signals  # noqa: E402
from web.publisher_repo import (  # noqa: E402
    normalize_recipients,
    record_email_delivery,
    workload_series,
)

DEFAULT_DB = ROOT / "web" / "daily_report.db"
DEFAULT_UPLOADS = ROOT / "uploads"
RULE = "─" * 58


def _out(text=""):
    print(text)


def _iso_date(value):
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from None
    return parsed


def load_user(connection, username=None):
    """挑一个发布者：显式指定 > 上次发布的人 > 第一个配了发件邮箱的人。"""
    columns = (
        "id, username, display_name, notification_email, notification_email_2,"
        " notification_email_3, sender_email, feishu_chat_id, feishu_chat_name"
    )
    if username:
        row = connection.execute(
            f"SELECT {columns} FROM publisher_users WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            raise SystemExit(f"没有找到用户 {username}")
        return dict(row)

    row = connection.execute(
        f"""SELECT {columns} FROM publisher_users
            WHERE id = (SELECT publisher_id FROM published_items ORDER BY id DESC LIMIT 1)"""
    ).fetchone()
    if row:
        return dict(row)
    row = connection.execute(
        f"""SELECT {columns} FROM publisher_users
            WHERE enabled = 1 AND sender_email != ''
            ORDER BY id LIMIT 1"""
    ).fetchone()
    if row:
        return dict(row)
    raise SystemExit("数据库里没有可用的发布者，请先用网页注册一个账号。")


def configured_recipients(user):
    values = [
        user.get("notification_email") or "",
        user.get("notification_email_2") or "",
        user.get("notification_email_3") or "",
    ]
    return [item for item in normalize_recipients(*values) if item]


def sync_feishu(user):
    """拉一次最近 5 分钟的群聊消息。失败只提示，不影响日报本身。"""
    from web.feishu_source import FeishuSourceError, collect_feishu_recent_messages
    from web.publisher_repo import persist_feishu_messages

    chat_id = (user.get("feishu_chat_id") or "").strip()
    app_id = (user.get("feishu_app_id") or "").strip()
    if not chat_id or not app_id:
        _out("飞书：还没有配置群聊，跳过同步。")
        return 0
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    try:
        chat_name, messages = collect_feishu_recent_messages(
            chat_id, app_id=app_id, chat_name=user.get("feishu_chat_name") or ""
        )
        completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with database_connection(str(DEFAULT_DB)) as connection:
            new_count = persist_feishu_messages(
                connection,
                messages,
                chat_id,
                chat_name,
                started,
                completed,
                user_id=user["id"],
                actor_name=user.get("display_name"),
            )
    except FeishuSourceError as error:
        _out(f"飞书同步失败（不影响发送）：{error}")
        return 0
    _out(f"飞书：拉取 {len(messages)} 条，新增 {new_count} 条。")
    return new_count


def today_delivery_count(connection, day):
    """当天已经成功发过几次日报，用来提醒重复发送。"""
    try:
        row = connection.execute(
            """SELECT COUNT(*) FROM email_delivery_logs
               WHERE work_date = ? AND delivery_type = 'daily_report' AND status = 'sent'""",
            (day.isoformat(),),
        ).fetchone()
    except Exception:
        return 0
    return int(row[0]) if row else 0


def build_content(report, status="已完成"):
    """和网页发布写进 published_items 的正文保持同一格式。"""
    sections = [f"工作状态：{status}"]
    if report.get("hours"):
        sections.append(f"投入工时：{report['hours']} 小时")
    body = []
    for label, key in (
        ("解决的问题", "solved"),
        ("完成事项", "finished"),
        ("问题与风险", "problems"),
        ("下一步计划", "next_steps"),
    ):
        value = (report.get(key) or "").strip()
        if value:
            body.append(f"{label}：\n{value}")
    if not body:
        return ""
    return "\n".join(sections) + "\n\n" + "\n\n".join(body)


def print_draft(report, signals, recipients, feishu_choice, user, sent_today=0):
    stats = report["sources"]
    _out()
    _out(RULE)
    _out(f"  {report['date']} 日报草稿 · {user['display_name']}")
    _out(RULE)
    _out(
        f"  提交 {stats['commits']} 次 · 改动 {stats['files']} 个文件 "
        f"(+{stats['additions']} / −{stats['deletions']}) · "
        f"AI 会话 {stats['prompts']} 轮 · 飞书 {stats['feishu']} 条"
    )
    _out()
    _out(f"  总述：{report['summary']}")
    for block_title, key in (
        ("今日完成", "finished"),
        ("解决的问题", "solved"),
        ("问题与风险", "problems"),
        ("下一步", "next_steps"),
    ):
        value = (report.get(key) or "").strip()
        if not value:
            continue
        _out()
        _out(f"  {block_title}")
        for line in value.splitlines():
            _out(f"    {line.lstrip('- ').strip()}")
    if report["tags"]:
        _out()
        _out("  标签：" + " ".join(report["tags"]))
    for warning in report["warnings"]:
        _out()
        _out(f"  注意：{warning}")
    if sent_today:
        _out()
        _out(f"  提醒：今天已发送过 {sent_today} 次，这次仍会带上当天全部提交。")
    _out()
    _out(RULE)
    _out("  收件人：" + ("、".join(recipients) if recipients else "（未配置）"))
    if signals["feishu"]:
        chat = signals["feishu"][0]["chat_name"] or "飞书群聊"
        _out(f"  飞书：{'附带' if feishu_choice else '不附带'}「{chat}」{len(signals['feishu'])} 条")
    elif user.get("feishu_chat_id"):
        _out("  飞书：当天没有已同步的消息（可加 --sync-feishu 拉一次最近的）")
    _out(RULE)


def ask(prompt, default=""):
    hint = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{hint}：").strip()
    except EOFError:
        return default
    return raw or default


def ask_yes(prompt, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{prompt} {hint}").lower()
    if not answer:
        return default
    return answer in ("y", "yes", "是", "好", "要")


def save_item(connection, user, report, attachments):
    content = build_content(report)
    if not content:
        return None
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    cursor = connection.execute(
        """INSERT INTO published_items
           (publisher_id, work_date, title, content, report_detail,
            attachments, source_url, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user["id"],
            report["date"],
            "今日工作进展",
            content,
            "标准",
            json.dumps(attachments, ensure_ascii=False),
            None,
            created_at,
        ),
    )
    return cursor.lastrowid


def deliver(user, recipients, report, signals, feishu_choice, attachments):
    """逐人发送并记录结果，和网页发布的发送口径一致。"""
    sender_email = (user.get("sender_email") or "").strip()
    settings = email_notifications.smtp_settings_from_env(sender_email)
    chat_name = signals["feishu"][0]["chat_name"] if signals["feishu"] else ""
    stream = []
    if feishu_choice:
        stream = [
            {"tag": "飞书群聊", "member": item["member"], "summary": item["summary"]}
            for item in signals["feishu"]
        ]
    subject = f"研一 {user['display_name']} {report['date']} 智能日报"
    sent, failed = [], []
    for recipient in recipients:
        error_message = ""
        try:
            email_notifications.send_progress_email(
                settings,
                recipient,
                user["display_name"],
                report["date"],
                report["finished"],
                report["problems"],
                report["next_steps"],
                status="已完成",
                stream_items=stream,
                stream_label=chat_name or None,
                report_detail="标准",
                attachments=attachments,
                solved=report["solved"],
                workload=report["workload"],
                hours=report["hours"],
            )
        except email_notifications.EmailNotificationError as error:
            error_message = str(error)
            failed.append((recipient, error_message))
        else:
            sent.append(recipient)
        with database_connection(str(DEFAULT_DB)) as connection:
            record_email_delivery(
                connection,
                user["id"],
                sender_email,
                recipient,
                subject,
                "daily_report",
                report["date"],
                "failed" if error_message else "sent",
                error_message,
            )
    return sent, failed


def install_hook(remove=False):
    hook = ROOT / ".git" / "hooks" / "post-commit"
    if remove:
        if hook.exists():
            hook.unlink()
            _out(f"已移除提交钩子：{hook}")
        else:
            _out("没有找到已安装的提交钩子。")
        return 0
    source = ROOT / "scripts" / "git-hooks" / "post-commit"
    if not source.exists():
        raise SystemExit(f"缺少钩子模板：{source}")
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    try:
        hook.chmod(0o755)
    except OSError:
        pass
    _out(f"已安装提交钩子：{hook}")
    _out("下次 git commit 成功后，终端会问你要不要发送当天日报。")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="在终端生成并发送今日日报，不经过网页",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", type=_iso_date, default=datetime.date.today(), help="默认今天")
    parser.add_argument("--user", help="发布者用户名，默认取上次发布的人")
    parser.add_argument("--to", help="收件邮箱，多个用逗号分隔")
    parser.add_argument("--hours", default="", help="投入工时（小时），不填则不计")
    parser.add_argument("--solved", default="", help="手工补充解决的问题，多条用 | 分隔")
    parser.add_argument("--attach", action="append", default=[], help="附件路径，可重复")
    parser.add_argument("--no-feishu", action="store_true", help="不附带当天飞书群聊")
    parser.add_argument("--sync-feishu", action="store_true", help="发送前先拉一次飞书群聊消息")
    parser.add_argument("--dry-run", action="store_true", help="只打印草稿，不写库不发信")
    parser.add_argument("--yes", "-y", action="store_true", help="不交互，直接发送到配置的邮箱")
    parser.add_argument("--install-hook", action="store_true", help="安装 git post-commit 钩子")
    parser.add_argument("--uninstall-hook", action="store_true", help="移除 git post-commit 钩子")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.install_hook or args.uninstall_hook:
        return install_hook(remove=args.uninstall_hook)

    if not DEFAULT_DB.exists():
        raise SystemExit(f"数据库不存在：{DEFAULT_DB}")

    with database_connection(str(DEFAULT_DB)) as connection:
        user = load_user(connection, args.user)

    if args.sync_feishu:
        sync_feishu(user)

    with database_connection(str(DEFAULT_DB)) as connection:
        signals = collect_signals(ROOT, connection=connection, day=args.date)
        report = build_report(signals, hours=args.hours)
        report["workload"] = workload_series(connection, args.date, days=7)
        sent_today = today_delivery_count(connection, args.date)
    if args.solved:
        report["solved"] = "\n".join(
            f"- {line.strip()}" for line in args.solved.split("|") if line.strip()
        )

    attachments = []
    for raw in args.attach:
        path = Path(raw)
        if not path.is_absolute():
            path = (ROOT / raw).resolve()
        if not path.exists():
            raise SystemExit(f"附件不存在：{path}")
        attachments.append(str(path))

    default_recipients = configured_recipients(user)
    feishu_choice = bool(signals["feishu"]) and not args.no_feishu

    if args.dry_run:
        print_draft(report, signals, default_recipients, feishu_choice, user, sent_today)
        _out("  dry-run：没有写入归档，也没有发送邮件。")
        return 0

    if not build_content(report):
        print_draft(report, signals, default_recipients, feishu_choice, user, sent_today)
        _out("今天没有采集到可发送的内容，先提交一次代码再试。")
        return 1

    recipients = list(default_recipients)
    if args.to:
        recipients = list(normalize_recipients(args.to))
    elif not args.yes:
        print_draft(report, signals, recipients, feishu_choice, user, sent_today)
        typed = ask(
            "收件邮箱（多个用逗号分隔，直接回车用默认）",
            "、".join(recipients) if recipients else "",
        )
        recipients = list(normalize_recipients(typed))
        if signals["feishu"] and not args.no_feishu:
            feishu_choice = ask_yes("附带今天的飞书群聊记录？", default=True)
        if not ask_yes("确认发送？", default=True):
            _out("已取消，没有写入归档。")
            return 0
    else:
        print_draft(report, signals, recipients, feishu_choice, user, sent_today)

    if not recipients:
        raise SystemExit("没有收件人。用 --to 指定，或先在网页里配置通知邮箱。")
    for recipient in recipients:
        if not email_notifications.is_valid_email(recipient):
            raise SystemExit(f"邮箱格式不正确：{recipient}")

    with database_connection(str(DEFAULT_DB)) as connection:
        item_id = save_item(connection, user, report, attachments)
    _out(f"已写入归档（#{item_id}）。")

    try:
        sent, failed = deliver(user, recipients, report, signals, feishu_choice, attachments)
    except email_notifications.EmailNotificationError as error:
        raise SystemExit(f"归档已保存，但邮件发送失败：{error}") from None

    if sent:
        _out("邮件已发送：" + "、".join(sent))
    for address, reason in failed:
        _out(f"发送失败：{address} —— {reason}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
