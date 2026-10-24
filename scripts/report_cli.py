#!/usr/bin/env python
"""在终端里生成并发送今日日报，不需要打开网页。

默认流程：读取当天的 Git 提交、本地代码改动、AI 会话提示词与飞书消息，
拼出一份日报草稿打印在终端；然后只问一次（回车发送 / r 改收件人 / e 改内容 / n 取消），
确认后写入归档并发送邮件。选 e 后直接报编号说明改哪一条、再填新内容；
也可以输入 a 新增、d 删除、h 改工时。

平时双击项目根目录的「发送日报.bat」即可，等价于下面第一条命令。

    py -3.12 scripts/report_cli.py                 # 今天，打印草稿后一步确认
    py -3.12 scripts/report_cli.py --dry-run       # 只看草稿，不发
    py -3.12 scripts/report_cli.py --date 2026-10-22
    py -3.12 scripts/report_cli.py --to a@x.com,b@y.com --yes
    py -3.12 scripts/report_cli.py --install-hook  # 每次 git commit 后询问
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for candidate in (ROOT, SCRIPTS_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from web import email_notifications  # noqa: E402
from web.app import database_connection  # noqa: E402
from web.daily_summary import build_report, collect_signals  # noqa: E402
from web.publisher_repo import (  # noqa: E402
    normalize_recipients,
    record_email_delivery,
    workload_series,
)

from report_editor import edit_one  # noqa: E402

DEFAULT_DB = ROOT / "web" / "daily_report.db"
DEFAULT_UPLOADS = ROOT / "uploads"
RULE = "─" * 58
CONFIG_PATH = Path(__file__).resolve().parent / "report_config.py"

CONFIG_DEFAULTS = {
    "USER": "",
    "RECIPIENTS": (),
    "HOURS": "",
    "REPORT_DETAIL": "标准",
    "SMTP_HOST": "",
    "SMTP_PORT": "",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_SECURE": "",
    "INCLUDE_FEISHU": True,
    "SYNC_FEISHU": False,
    "AUTO_SEND": False,
}


def load_config(path=None):
    """读取 scripts/report_config.py；文件缺失或写错时退回默认值，不让发送流程中断。"""
    source = Path(path) if path else CONFIG_PATH
    values = dict(CONFIG_DEFAULTS)
    if not source.exists():
        return values
    namespace = {}
    try:
        code = compile(source.read_text(encoding="utf-8"), str(source), "exec")
        exec(code, namespace)  # noqa: S102 - 读的是仓库内的配置文件
    except (OSError, SyntaxError, ValueError) as error:
        print(f"配置文件读取失败，改用默认值：{error}")
        return values
    for key in values:
        if key in namespace:
            values[key] = namespace[key]
    if values["REPORT_DETAIL"] not in {"简洁", "标准", "详细"}:
        values["REPORT_DETAIL"] = "标准"
    recipients = values["RECIPIENTS"]
    if isinstance(recipients, str):
        recipients = [item.strip() for item in recipients.split(",") if item.strip()]
    values["RECIPIENTS"] = tuple(str(item).strip() for item in recipients if str(item).strip())
    values["HOURS"] = "" if values["HOURS"] is None else str(values["HOURS"]).strip()
    values["USER"] = str(values["USER"] or "").strip()
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_SECURE"):
        values[key] = "" if values[key] is None else str(values[key]).strip()
    values["INCLUDE_FEISHU"] = bool(values["INCLUDE_FEISHU"])
    values["SYNC_FEISHU"] = bool(values["SYNC_FEISHU"])
    values["AUTO_SEND"] = bool(values["AUTO_SEND"])
    return values


CONFIG = load_config()


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


CANCEL_WORDS = {"n", "no", "q", "quit", "取消", "不发", "退出"}
EDIT_WORDS = {"e", "edit", "改", "编辑"}
RECIPIENT_WORDS = {"r", "recipient", "recipients", "to", "收件人"}
SEND = "send"
EDIT = "edit"
CANCEL = "cancel"
RECIPIENT_SEPARATORS = re.compile(r"[,，;；\s]+")


def parse_recipients(text):
    """把「a@x.com, b@y.com」这类输入拆成邮箱列表。

    normalize_recipients 是变参函数，只接受单个地址；这里负责拆分再交给它校验。
    """
    parts = [part for part in RECIPIENT_SEPARATORS.split(text or "") if part]
    return list(normalize_recipients(*parts))


def _ask_recipients(current):
    """在确认环节单独改收件人；填错只提示，保持原来的列表。"""
    _out("  新的收件邮箱（多个用逗号隔开，回车不改）：")
    try:
        typed = input("  > ").strip()
    except EOFError:
        typed = ""
    if not typed:
        return current
    try:
        return parse_recipients(typed)
    except email_notifications.EmailNotificationError as error:
        _out(f"  {error}")
        return current


def ask_send_target(recipients):
    """一步确认：回车发送 / r 改收件人 / 直接输邮箱改发 / e 改内容 / n 取消。

    返回 (动作, 收件人列表)，动作是 SEND / EDIT / CANCEL。
    r 改过的收件人会跟着返回，所以先改收件人再按 e 改内容也不会丢。
    """
    current = list(recipients)
    while True:
        _out()
        _out("  收件人：" + ("、".join(current) if current else "（还没配置）"))
        _out("  回车发送 · r 改收件人 · e 改内容 · n 取消")
        try:
            raw = input("  > ").strip()
        except EOFError:
            raw = ""
        lowered = raw.lower()
        if lowered in CANCEL_WORDS:
            return CANCEL, list(current)
        if lowered in EDIT_WORDS:
            return EDIT, list(current)
        if lowered in RECIPIENT_WORDS:
            current = _ask_recipients(current)
            continue
        if not raw:
            return SEND, list(current)
        try:
            return SEND, parse_recipients(raw)
        except email_notifications.EmailNotificationError as error:
            _out(f"  {error}")
            _out("  请重新输入。")


def save_item(connection, user, report, attachments, report_detail="标准"):
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
            report_detail,
            json.dumps(attachments, ensure_ascii=False),
            None,
            created_at,
        ),
    )
    return cursor.lastrowid


def smtp_settings_for(user, config=None):
    """取发信配置：配置文件里写了 SMTP_HOST 就用它，否则读本机 .smtp.local.json。"""
    config = CONFIG if config is None else config
    host = str(config.get("SMTP_HOST") or "").strip()
    sender_email = (user.get("sender_email") or "").strip()
    if not host:
        return email_notifications.smtp_settings_from_env(sender_email)

    username = str(config.get("SMTP_USERNAME") or "").strip() or sender_email
    password = str(config.get("SMTP_PASSWORD") or "")
    if not username or not password:
        raise email_notifications.EmailNotificationError(
            f"{CONFIG_PATH.name} 里填了 SMTP_HOST，但 SMTP_USERNAME 或 SMTP_PASSWORD 是空的。"
        )
    port_text = str(config.get("SMTP_PORT") or "").strip() or "465"
    try:
        port = int(port_text)
    except ValueError:
        raise email_notifications.EmailNotificationError("SMTP_PORT 必须是数字。") from None
    secure = str(config.get("SMTP_SECURE") or "").strip().lower()
    if secure not in {"ssl", "starttls", "none"}:
        secure = "ssl" if port == 465 else "starttls"
    return email_notifications.SmtpSettings(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender_email or username,
        secure=secure,
    )


def deliver(
    user, recipients, report, signals, feishu_choice, attachments, report_detail="标准"
):
    """逐人发送并记录结果，和网页发布的发送口径一致。"""
    settings = smtp_settings_for(user)
    sender_email = (
        getattr(settings, "sender", "") if settings else ""
    ) or (user.get("sender_email") or "").strip()
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
                report_detail=report_detail,
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
    parser.add_argument(
        "--user", default=CONFIG["USER"] or None, help="发布者用户名，默认取上次发布的人"
    )
    parser.add_argument("--to", help="收件邮箱，多个用逗号分隔")
    parser.add_argument("--hours", default=CONFIG["HOURS"], help="投入工时（小时），不填则不计")
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

    config = CONFIG
    report_detail = config["REPORT_DETAIL"]

    if not DEFAULT_DB.exists():
        raise SystemExit(f"数据库不存在：{DEFAULT_DB}")

    with database_connection(str(DEFAULT_DB)) as connection:
        user = load_user(connection, args.user)

    if args.sync_feishu or config["SYNC_FEISHU"]:
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

    config_recipients = list(config["RECIPIENTS"])
    default_recipients = config_recipients or configured_recipients(user)
    feishu_choice = (
        bool(signals["feishu"]) and not args.no_feishu and config["INCLUDE_FEISHU"]
    )
    auto_send = bool(args.yes or config["AUTO_SEND"])

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
        recipients = parse_recipients(args.to)

    print_draft(report, signals, recipients, feishu_choice, user, sent_today)
    if not auto_send:
        while True:
            action, recipients = ask_send_target(recipients)
            if action == CANCEL:
                _out("已取消，没有写入归档。")
                return 0
            if action == EDIT:
                edit_one(report, writer=_out)
                if not build_content(report):
                    _out("  四段都空了，没有内容可发，已取消。")
                    return 0
                continue
            break

    if not recipients:
        raise SystemExit(
            f"没有收件人。改 {CONFIG_PATH.name} 里的 RECIPIENTS，或用 --to 指定。"
        )
    for recipient in recipients:
        if not email_notifications.is_valid_email(recipient):
            raise SystemExit(f"邮箱格式不正确：{recipient}")

    with database_connection(str(DEFAULT_DB)) as connection:
        item_id = save_item(connection, user, report, attachments, report_detail)
    _out(f"已写入归档（#{item_id}）。")

    try:
        sent, failed = deliver(
            user, recipients, report, signals, feishu_choice, attachments, report_detail
        )
    except email_notifications.EmailNotificationError as error:
        raise SystemExit(f"归档已保存，但邮件发送失败：{error}") from None

    if sent:
        _out("邮件已发送：" + "、".join(sent))
    for address, reason in failed:
        _out(f"发送失败：{address} —— {reason}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
