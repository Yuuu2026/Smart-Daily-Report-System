"""第二套系统：登录后发布信息，数据写入日报系统共享 SQLite。"""

import argparse
import datetime
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.app import DATABASE_PATH, database_connection, initialize_database
from web.email_notifications import (
    EmailNotificationError,
    is_valid_email,
    local_sender_email,
    send_progress_email,
    send_test_email,
    smtp_password_is_saved,
    smtp_settings_from_env,
    store_smtp_password,
    write_local_smtp_config,
)
from web.audit import log_audit
from web.github_source import (
    GitHubSourceError,
    collect_last_24_hours,
    normalize_repository,
    validate_repository,
)
from web.feishu_source import (
    FeishuSourceError,
    collect_feishu_recent_messages,
    list_feishu_chats,
    store_feishu_app_secret,
)
from web.publisher_auth import hash_password, verify_password
from report_core import parse_progress_sections
from report_quality import quality_warnings


ARCHIVE_PAGE_SIZE = 6
GITHUB_PAGE_SIZE = 8
FEISHU_PAGE_SIZE = 8
FEISHU_AUTO_SYNC_INTERVAL_SECONDS = 5
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_ATTACHMENT_COUNT = 6
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024


def persist_feishu_messages(
    connection,
    messages,
    chat_id,
    chat_name,
    started,
    completed,
    user_id=None,
    actor_name=None,
):
    """将飞书消息写入 feishu_messages / records，并记录同步运行与审计。"""
    new_count = 0
    for message in messages:
        extra_fields = {
            "source_type": "feishu",
            "message_id": message["message_id"],
            "chat_id": message["chat_id"],
            "sender_id": message["sender_id"],
            "msg_type": message["msg_type"],
            "created_at_utc": message["created_at_utc"],
            "collected_at_utc": message["collected_at_utc"],
        }
        extra_json = json.dumps(extra_fields, ensure_ascii=False, sort_keys=True)
        existing = connection.execute(
            "SELECT id, record_id FROM feishu_messages WHERE message_id = ?",
            (message["message_id"],),
        ).fetchone()
        if existing:
            record_id = existing["record_id"]
            if record_id:
                connection.execute(
                    """UPDATE records
                       SET member = ?, channel = ?, summary = ?, extra_json = ?
                       WHERE id = ?""",
                    (
                        message["member_name"],
                        message["chat_name"],
                        message["summary"],
                        extra_json,
                        record_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """INSERT INTO records
                       (work_date, category, member, channel, summary, extra_json)
                       VALUES (?, 'collaborations', ?, ?, ?, ?)""",
                    (
                        message["work_date"],
                        message["member_name"],
                        message["chat_name"],
                        message["summary"],
                        extra_json,
                    ),
                )
                record_id = cursor.lastrowid
            connection.execute(
                """UPDATE feishu_messages
                   SET chat_name = ?, member_name = ?, msg_type = ?,
                       work_date = ?, summary = ?, created_at_utc = ?,
                       collected_at_utc = ?, record_id = ?
                   WHERE id = ?""",
                (
                    message["chat_name"],
                    message["member_name"],
                    message["msg_type"],
                    message["work_date"],
                    message["summary"],
                    message["created_at_utc"],
                    message["collected_at_utc"],
                    record_id,
                    existing["id"],
                ),
            )
            continue

        cursor = connection.execute(
            """INSERT INTO records
               (work_date, category, member, channel, summary, extra_json)
               VALUES (?, 'collaborations', ?, ?, ?, ?)""",
            (
                message["work_date"],
                message["member_name"],
                message["chat_name"],
                message["summary"],
                extra_json,
            ),
        )
        record_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO feishu_messages
               (message_id, chat_id, chat_name, sender_id, member_name,
                msg_type, work_date, summary, created_at_utc,
                collected_at_utc, record_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message["message_id"],
                message["chat_id"],
                message["chat_name"],
                message["sender_id"],
                message["member_name"],
                message["msg_type"],
                message["work_date"],
                message["summary"],
                message["created_at_utc"],
                message["collected_at_utc"],
                record_id,
            ),
        )
        new_count += 1

    connection.execute(
        """INSERT INTO feishu_sync_runs
           (chat_id, chat_name, status, message_count, started_at_utc, completed_at_utc)
           VALUES (?, ?, 'succeeded', ?, ?, ?)""",
        (chat_id, chat_name, len(messages), started, completed),
    )
    if user_id:
        connection.execute(
            """UPDATE publisher_users
               SET feishu_chat_id = ?, feishu_chat_name = ?
               WHERE id = ?""",
            (chat_id, chat_name, user_id),
        )
    log_audit(
        connection,
        "feishu_sync_succeeded",
        "feishu_chat",
        actor_id=user_id,
        actor_name=actor_name or "自动同步",
        work_date=datetime.date.today().isoformat(),
        summary={
            "message_count": len(messages),
            "new_count": new_count,
            "trigger": "auto" if not user_id else "manual",
        },
    )
    return new_count


def _stream_for_publish(connection, selected_source, chat_id, repository, work_date=None):
    """根据当前数据源查询流水，返回 (stream_label, stream_items)。"""
    seen = set()

    def unique(item):
        key = (item["member"], item["summary"])
        if key in seen:
            return None
        seen.add(key)
        return item

    if selected_source == "feishu" and chat_id:
        if work_date:
            rows = connection.execute(
                """SELECT member_name, summary FROM feishu_messages
                   WHERE chat_id = ? AND work_date = ?
                   ORDER BY created_at_utc ASC, id ASC""",
                (chat_id, work_date),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT member_name, summary FROM feishu_messages
                   WHERE chat_id = ? ORDER BY created_at_utc ASC, id ASC""",
                (chat_id,),
            ).fetchall()
        items = [
            {"tag": "飞书", "member": row["member_name"], "summary": row["summary"]}
            for row in rows
        ]
        items = [item for item in (unique(item) for item in items) if item]
        return "飞书群聊消息", items[-80:]
    if selected_source == "github" and repository:
        if work_date:
            rows = connection.execute(
                """SELECT member_name, message FROM github_commits
                   WHERE repository = ? AND work_date = ?
                   ORDER BY committed_at_utc ASC, id ASC""",
                (repository, work_date),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT member_name, message FROM github_commits
                   WHERE repository = ? ORDER BY committed_at_utc ASC, id ASC""",
                (repository,),
            ).fetchall()
        items = [
            {"tag": "GitHub", "member": row["member_name"], "summary": row["message"]}
            for row in rows
        ]
        items = [item for item in (unique(item) for item in items) if item]
        return "GitHub 提交", items[-80:]
    return None, []


_feishu_auto_sync_stop = None
_feishu_auto_sync_last_failure = {}


def _feishu_auto_sync_run_once(database_path):
    """对所有已配置飞书群聊的用户执行一次消息拉取。"""
    try:
        with database_connection(database_path) as connection:
            users = connection.execute(
                """SELECT id, display_name, feishu_app_id, feishu_chat_id, feishu_chat_name
                   FROM publisher_users
                   WHERE feishu_app_id != '' AND feishu_chat_id != ''
                   ORDER BY id"""
            ).fetchall()
    except sqlite3.Error as error:
        print(f"[飞书自动同步] 读取用户配置失败：{error}", file=sys.stderr)
        return

    for user in users:
        started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        try:
            chat_name, messages = collect_feishu_recent_messages(
                user["feishu_chat_id"],
                app_id=user["feishu_app_id"],
                chat_name=user["feishu_chat_name"],
            )
            completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            with database_connection(database_path) as connection:
                new_count = persist_feishu_messages(
                    connection,
                    messages,
                    user["feishu_chat_id"],
                    chat_name,
                    started,
                    completed,
                    user_id=user["id"],
                    actor_name=user["display_name"],
                )
            print(
                f"[飞书自动同步] 用户 {user['display_name']} 群「{chat_name}」："
                f"获取 {len(messages)} 条，新增 {new_count} 条。"
            )
            _feishu_auto_sync_last_failure.pop(user["id"], None)
        except FeishuSourceError as error:
            completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            error_message = str(error)[:240]
            failure_signature = error_message
            previous_failure = _feishu_auto_sync_last_failure.get(user["id"])
            _feishu_auto_sync_last_failure[user["id"]] = failure_signature
            if previous_failure == failure_signature:
                continue
            try:
                with database_connection(database_path) as connection:
                    connection.execute(
                        """INSERT INTO feishu_sync_runs
                           (chat_id, chat_name, status, message_count, started_at_utc,
                            completed_at_utc, error_message)
                           VALUES (?, ?, 'failed', 0, ?, ?, ?)""",
                        (
                            user["feishu_chat_id"],
                            user["feishu_chat_name"] or user["feishu_chat_id"],
                            started,
                            completed,
                            error_message,
                        ),
                    )
                    log_audit(
                        connection,
                        "feishu_sync_failed",
                        "feishu_chat",
                        actor_id=user["id"],
                        actor_name=user["display_name"],
                        summary={"error_code": "source_unavailable"},
                    )
            except sqlite3.Error as db_error:
                print(f"[飞书自动同步] 写入失败记录出错：{db_error}", file=sys.stderr)
            print(
                f"[飞书自动同步] 用户 {user['display_name']} 同步失败：{error}",
                file=sys.stderr,
            )


def _feishu_auto_sync_worker(database_path, stop_event):
    """后台线程：周期性拉取所有已配置用户的飞书群聊消息。"""
    interval = FEISHU_AUTO_SYNC_INTERVAL_SECONDS
    print(
        f"[飞书自动同步] 后台线程已启动，每 {FEISHU_AUTO_SYNC_INTERVAL_SECONDS} 秒拉取一次。"
    )
    while not stop_event.is_set():
        try:
            _feishu_auto_sync_run_once(database_path)
        except Exception as error:
            print(f"[飞书自动同步] 未预期错误：{error}", file=sys.stderr)
        if stop_event.wait(interval):
            break
    print("[飞书自动同步] 后台线程已停止。")


def start_feishu_auto_sync(database_path=DATABASE_PATH):
    """启动飞书自动同步后台线程（幂等，多次调用只启动一次）。"""
    global _feishu_auto_sync_stop
    if _feishu_auto_sync_stop is not None:
        return
    import threading

    stop_event = threading.Event()
    _feishu_auto_sync_stop = stop_event
    thread = threading.Thread(
        target=_feishu_auto_sync_worker,
        args=(database_path, stop_event),
        name="feishu-auto-sync",
        daemon=True,
    )
    thread.start()


def stop_feishu_auto_sync():
    """停止飞书自动同步后台线程。"""
    global _feishu_auto_sync_stop
    if _feishu_auto_sync_stop is not None:
        _feishu_auto_sync_stop.set()
        _feishu_auto_sync_stop = None


def _parse_date(value):
    if not value:
        return None
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期必须是有效的 YYYY-MM-DD 日期。") from None
    if parsed.isoformat() != value:
        raise ValueError("日期必须是 YYYY-MM-DD 格式。")
    return parsed


def _date_window(from_value, to_value, default_days=30):
    if not from_value and not to_value:
        return "", ""
    today = datetime.date.today()
    start = _parse_date(from_value) or today - datetime.timedelta(days=default_days)
    end = _parse_date(to_value) or today
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    return start.isoformat(), end.isoformat()


def _parse_page(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _relative_time(value):
    committed_at = datetime.datetime.fromisoformat(value)
    if committed_at.tzinfo is None:
        committed_at = committed_at.replace(tzinfo=datetime.timezone.utc)
    elapsed = max(
        0,
        int((datetime.datetime.now(datetime.timezone.utc) - committed_at).total_seconds()),
    )
    if elapsed < 3600:
        return f"{max(1, elapsed // 60)} 分钟前"
    if elapsed < 86400:
        return f"{elapsed // 3600} 小时前"
    if elapsed < 172800:
        return "昨天"
    return committed_at.astimezone().strftime("%m月%d日")


def _record_email_delivery(
    connection,
    user_id,
    sender_email,
    recipient_email,
    subject,
    delivery_type,
    work_date,
    status,
    error_message="",
):
    """记录一次邮件发送结果，供工作台展示最近发送状态。"""
    connection.execute(
        """INSERT INTO email_delivery_logs (
            user_id, sender_email, recipient_email, subject, delivery_type,
            status, work_date, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            sender_email or "",
            recipient_email or "",
            subject,
            delivery_type,
            status,
            work_date,
            error_message or None,
            datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def _normalize_recipients(*emails):
    """规范化多个收件邮箱：去空、去重、校验格式，返回有序列表。"""
    recipients = []
    seen = set()
    for email in emails:
        address = (email or "").strip()
        if not address:
            continue
        if not is_valid_email(address):
            raise EmailNotificationError(f"收件邮箱格式不正确：{address}")
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(address)
    return recipients


def _save_uploaded_attachments(files, upload_folder):
    """保存日报照片，返回相对上传目录的 POSIX 路径列表。"""
    if not files:
        return []
    upload_root = Path(upload_folder)
    saved = []
    for file in files:
        if not file or not file.filename:
            continue
        if len(saved) >= MAX_ATTACHMENT_COUNT:
            break
        extension = Path(file.filename).suffix.lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("仅支持 JPG、PNG、GIF、WebP、BMP 图片。")
        content_type = (file.content_type or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError("上传文件不是图片，请重新选择照片。")
        file.stream.seek(0, os.SEEK_END)
        size = file.stream.tell()
        file.stream.seek(0)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError("单张照片不能超过 6MB。")
        now = datetime.date.today()
        target_dir = upload_root / str(now.year) / f"{now.month:02d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{secrets.token_hex(16)}{extension}"
        try:
            file.save(str(target_dir / filename))
        except OSError as error:
            raise ValueError("照片保存失败，请检查项目目录权限。") from error
        finally:
            file.stream.close()
        saved.append((target_dir / filename).relative_to(upload_root).as_posix())
    return saved


def _attachment_urls(raw_attachments):
    """把数据库中的 JSON 附件路径转成可直接访问的 URL。"""
    try:
        attachments = json.loads(raw_attachments or "[]")
    except (TypeError, ValueError):
        attachments = []
    if not isinstance(attachments, list):
        return []
    return [
        f"/uploads/{str(item).lstrip('/')}"
        for item in attachments
        if str(item).strip()
    ]


def _published_item(row):
    created_at = datetime.datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    local_created_at = created_at.astimezone()
    title = row["title"].strip() or "今日工作进展"
    content = (row["content"] or "").strip()
    sections = parse_progress_sections(content)
    section_items = [
        {"label": label, "text": sections[field]}
        for field, label in (
            ("finished", "完成事项"),
            ("problems", "问题与风险"),
            ("next_steps", "下一步计划"),
        )
        if sections[field]
    ]
    return {
        "id": f"publish-{row['id']}",
        "work_date": row["work_date"],
        "created_at": row["created_at"],
        "local_created_at": local_created_at.strftime("%Y-%m-%d %H:%M"),
        "relative_time": _relative_time(row["created_at"]),
        "title": title,
        "content": content,
        "status": sections["status"] or "未标记状态",
        "report_detail": row["report_detail"] if "report_detail" in row.keys() else "标准",
        "attachments": _attachment_urls(
            row["attachments"] if "attachments" in row.keys() else "[]"
        ),
        "sections": section_items,
        "source_url": row["source_url"],
        "member": row["member"],
        "source_type": "成员日报",
        "repository": "",
        "sha_short": "",
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
    }


def _github_item(row):
    committed_at = datetime.datetime.fromisoformat(row["committed_at_utc"])
    if committed_at.tzinfo is None:
        committed_at = committed_at.replace(tzinfo=datetime.timezone.utc)
    local_created_at = committed_at.astimezone()
    message = (row["message"] or "").strip()
    return {
        "id": f"github-{row['id']}",
        "work_date": row["work_date"],
        "created_at": row["committed_at_utc"],
        "local_created_at": local_created_at.strftime("%Y-%m-%d %H:%M"),
        "relative_time": _relative_time(row["committed_at_utc"]),
        "title": message.splitlines()[0] if message else "无提交说明",
        "content": message,
        "source_url": row["commit_url"],
        "member": row["member"],
        "source_type": "GitHub 提交",
        "repository": row["repository"],
        "sha_short": (row["sha"] or "")[:7],
        "additions": row["additions"],
        "deletions": row["deletions"],
        "changed_files": row["changed_files"],
    }


def _feishu_item(row):
    created_at = datetime.datetime.fromisoformat(row["created_at_utc"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    local_created_at = created_at.astimezone()
    summary = (row["summary"] or "").strip()
    title = " ".join(summary.split())[:80] or "无消息内容"
    return {
        "id": f"feishu-{row['id']}",
        "work_date": row["work_date"],
        "created_at": row["created_at_utc"],
        "local_created_at": local_created_at.strftime("%Y-%m-%d %H:%M"),
        "relative_time": _relative_time(row["created_at_utc"]),
        "title": title,
        "content": summary,
        "source_url": "",
        "member": row["member_name"],
        "source_type": "飞书群聊",
        "repository": "",
        "sha_short": "",
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
        "status": row["msg_type"] or "消息",
        "channel": row["chat_name"],
        "message_type": row["msg_type"] or "消息",
    }


def create_publisher_app(database_path=DATABASE_PATH, secret_key=None):
    app = Flask(
        __name__,
        template_folder="publisher_templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.secret_key = secret_key or os.environ.get("DAILY_REPORT_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(
        DATABASE_PATH=str(database_path),
        UPLOAD_FOLDER=str(PROJECT_ROOT / "uploads"),
        MAX_CONTENT_LENGTH=MAX_ATTACHMENT_BYTES * MAX_ATTACHMENT_COUNT,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("DAILY_REPORT_COOKIE_SECURE") == "1",
    )
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    def csrf_token():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    @app.context_processor
    def inject_template_context():
        notifications = []
        unread_notifications = 0
        if "user_id" in session:
            try:
                with database_connection(app.config["DATABASE_PATH"]) as connection:
                    rows = connection.execute(
                        """SELECT id, title, created_at, read_at
                           FROM user_notifications
                           WHERE user_id = ?
                           ORDER BY created_at DESC, id DESC
                           LIMIT 6""",
                        (session["user_id"],),
                    ).fetchall()
                for row in rows:
                    created_at = datetime.datetime.fromisoformat(row["created_at"])
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
                    if not row["read_at"]:
                        unread_notifications += 1
                    notifications.append({
                        "id": row["id"],
                        "title": row["title"],
                        "read": bool(row["read_at"]),
                        "created_at": created_at.astimezone().strftime("%m月%d日 %H:%M"),
                    })
            except sqlite3.Error:
                notifications = []
                unread_notifications = 0
        return {
            "csrf_token": csrf_token(),
            "notifications": notifications,
            "unread_notifications": unread_notifications,
            "current_user": {
                "id": session.get("user_id", ""),
                "display_name": session.get("display_name", ""),
                "username": session.get("username", ""),
            },
        }

    def csrf_valid():
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))

    def save_email_channel(
        sender_email,
        recipient_email,
        smtp_password,
        recipient_email_2="",
        recipient_email_3="",
    ):
        sender_email = (sender_email or "").strip()
        recipients = _normalize_recipients(
            recipient_email,
            recipient_email_2,
            recipient_email_3,
        )
        smtp_password = smtp_password or ""
        if sender_email and not is_valid_email(sender_email):
            raise EmailNotificationError("发件邮箱格式不正确。")
        if smtp_password:
            store_smtp_password(sender_email, smtp_password)
        if sender_email:
            write_local_smtp_config(sender_email)
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            connection.execute(
                """UPDATE publisher_users
                   SET sender_email = ?, notification_email = ?,
                       notification_email_2 = ?, notification_email_3 = ?
                   WHERE id = ?""",
                (
                    sender_email,
                    recipients[0] if len(recipients) > 0 else "",
                    recipients[1] if len(recipients) > 1 else "",
                    recipients[2] if len(recipients) > 2 else "",
                    session["user_id"],
                ),
            )
        session["sender_email"] = sender_email
        session["notification_email"] = recipients[0] if recipients else ""
        session["notification_email_2"] = (
            recipients[1] if len(recipients) > 1 else ""
        )
        session["notification_email_3"] = (
            recipients[2] if len(recipients) > 2 else ""
        )
        return recipients

    def deliver_progress_email(
        sender_email,
        recipients,
        delivery_type,
        work_date,
        finished,
        problems,
        next_steps,
        status="已完成",
        source_url="",
        stream_items=None,
        stream_label=None,
        report_detail="标准",
        attachments=None,
    ):
        """给多个收件人分别发送邮件，并逐条记录发送结果。"""
        settings = smtp_settings_from_env(sender_email)
        subject = f"研一 {session['display_name']} {work_date} 智能日报"
        sent = []
        failed = []
        for recipient in recipients:
            try:
                send_progress_email(
                    settings,
                    recipient,
                    session["display_name"],
                    work_date,
                    finished,
                    problems,
                    next_steps,
                    status=status,
                    source_url=source_url,
                    stream_items=stream_items,
                    stream_label=stream_label,
                    report_detail=report_detail,
                    attachments=attachments,
                )
            except EmailNotificationError as error:
                with database_connection(app.config["DATABASE_PATH"]) as connection:
                    _record_email_delivery(
                        connection,
                        session["user_id"],
                        sender_email,
                        recipient,
                        subject,
                        delivery_type,
                        work_date,
                        "failed",
                        str(error),
                    )
                failed.append((recipient, str(error)))
            else:
                with database_connection(app.config["DATABASE_PATH"]) as connection:
                    _record_email_delivery(
                        connection,
                        session["user_id"],
                        sender_email,
                        recipient,
                        subject,
                        delivery_type,
                        work_date,
                        "sent",
                    )
                sent.append(recipient)
        return sent, failed

    @app.get("/uploads/<path:filename>")
    def uploaded_file(filename):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.get("/sources")
    def select_source():
        if "user_id" not in session:
            return redirect(url_for("login"))

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            user_row = connection.execute(
                """SELECT feishu_app_id, feishu_chat_id, feishu_chat_name
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()

        feishu_chat_id = session.get("selected_feishu_chat_id") or (
            user_row["feishu_chat_id"] if user_row else ""
        )
        feishu_chat_name = session.get("selected_feishu_chat_name") or (
            user_row["feishu_chat_name"] if user_row else ""
        )
        selected_source = session.get("selected_source", "")
        selected_repository = session.get("selected_repository", "")

        return render_template(
            "sources.html",
            selected_source=selected_source,
            selected_repository=selected_repository,
            feishu_app_id=session.get("feishu_app_id") or (
                user_row["feishu_app_id"] if user_row else ""
            ),
            feishu_chat_id=feishu_chat_id,
            feishu_chat_name=feishu_chat_name,
            github_selected=selected_source == "github" and bool(selected_repository),
            feishu_selected=selected_source == "feishu" and bool(feishu_chat_id),
            active_page="sources",
            page_title="接入设置",
            breadcrumb_title="接入设置",
            breadcrumb_subtitle="选择日报数据接口",
            search_target=url_for("archive_page"),
        )

    @app.get("/")
    def home():
        if "user_id" not in session:
            return redirect(url_for("login"))
        source_switch = request.args.get("source")
        if source_switch in ("github", "feishu"):
            session["selected_source"] = source_switch
            return redirect(url_for("home"))
        selected_source = session.get("selected_source")
        selected_repository = session.get("selected_repository")
        selected_feishu_chat_id = session.get("selected_feishu_chat_id")
        if not selected_source:
            if selected_repository:
                session["selected_source"] = "github"
                selected_source = "github"
            elif selected_feishu_chat_id:
                session["selected_source"] = "feishu"
                selected_source = "feishu"
            else:
                flash("请先选择日报数据接口。", "warning")
                return redirect(url_for("select_source"))
        if selected_source == "github" and not selected_repository:
            flash("请先在接入设置中选择 GitHub 仓库。", "warning")
            return redirect(url_for("select_source"))
        # 飞书已选定但未配置群聊时，不再强制跳转到接入设置；
        # 直接渲染工作台，并在工作台内显示「群聊接入」引导卡片。
        feishu_needs_setup = bool(
            selected_source == "feishu" and not selected_feishu_chat_id
        )
        publish_only = request.args.get("publish_only") == "1"
        today = datetime.date.today()
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            commit_count = 0
            feishu_message_count = 0
            if selected_source == "github" and selected_repository:
                commit_count = connection.execute(
                    "SELECT COUNT(*) FROM github_commits WHERE repository = ? AND work_date = ?",
                    (selected_repository, today.isoformat()),
                ).fetchone()[0]
            if selected_source == "feishu" and selected_feishu_chat_id:
                feishu_message_count = connection.execute(
                    "SELECT COUNT(*) FROM feishu_messages WHERE chat_id = ? AND work_date = ?",
                    (selected_feishu_chat_id, today.isoformat()),
                ).fetchone()[0]
            task_count = connection.execute(
                "SELECT COUNT(*) FROM records WHERE category = 'tasks' AND work_date = ?",
                (today.isoformat(),),
            ).fetchone()[0]
            published_count = connection.execute(
                "SELECT COUNT(*) FROM published_items WHERE work_date = ?",
                (today.isoformat(),),
            ).fetchone()[0]
            collaboration_count = connection.execute(
                "SELECT COUNT(*) FROM records WHERE category = 'collaborations' AND work_date = ?",
                (today.isoformat(),),
            ).fetchone()[0]
            activity_items = []
            activity_count = 0
            sync_status = None
            if selected_source == "github" and selected_repository:
                activity_rows = connection.execute(
                    """SELECT id, repository, sha, work_date, message, additions,
                              deletions, changed_files, commit_url, committed_at_utc,
                              member_name AS member
                       FROM github_commits
                       WHERE repository = ?
                       ORDER BY committed_at_utc DESC, id DESC
                       LIMIT 12""",
                    (selected_repository,),
                ).fetchall()
                activity_items = [_github_item(row) for row in activity_rows]
                activity_count = connection.execute(
                    "SELECT COUNT(*) FROM github_commits WHERE repository = ?",
                    (selected_repository,),
                ).fetchone()[0]
                sync_status = connection.execute(
                    """SELECT status, commit_count, completed_at_utc, error_message
                       FROM github_sync_runs
                       WHERE repository = ?
                       ORDER BY id DESC LIMIT 1""",
                    (selected_repository,),
                ).fetchone()
            elif selected_source == "feishu" and selected_feishu_chat_id:
                activity_rows = connection.execute(
                    """SELECT id, message_id, chat_id, chat_name, sender_id,
                              member_name, msg_type, work_date, summary,
                              created_at_utc, collected_at_utc
                       FROM feishu_messages
                       WHERE chat_id = ?
                       ORDER BY created_at_utc DESC, id DESC
                       LIMIT 12""",
                    (selected_feishu_chat_id,),
                ).fetchall()
                activity_items = [_feishu_item(row) for row in activity_rows]
                activity_count = connection.execute(
                    "SELECT COUNT(*) FROM feishu_messages WHERE chat_id = ?",
                    (selected_feishu_chat_id,),
                ).fetchone()[0]
                sync_status = connection.execute(
                    """SELECT status, message_count, completed_at_utc, error_message
                       FROM feishu_sync_runs
                       WHERE chat_id = ?
                       ORDER BY id DESC LIMIT 1""",
                    (selected_feishu_chat_id,),
                ).fetchone()
            email_row = connection.execute(
                """SELECT sender_email, notification_email,
                          notification_email_2, notification_email_3
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()
            delivery_rows = connection.execute(
                """SELECT sender_email, recipient_email, subject, delivery_type,
                          status, work_date, error_message, created_at
                   FROM email_delivery_logs
                   WHERE user_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT 5""",
                (session["user_id"],),
            ).fetchall()
        sender_email = email_row["sender_email"] or local_sender_email()
        notification_email = email_row["notification_email"]
        notification_email_2 = email_row["notification_email_2"]
        notification_email_3 = email_row["notification_email_3"]
        email_deliveries = []
        delivery_type_labels = {
            "daily_report": "日报",
            "github_sync": "GitHub同步",
            "feishu_sync": "飞书同步",
        }
        for delivery in delivery_rows:
            created_at = datetime.datetime.fromisoformat(delivery["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.timezone.utc)
            email_deliveries.append({
                "sender_email": delivery["sender_email"],
                "recipient_email": delivery["recipient_email"],
                "subject": delivery["subject"],
                "delivery_type": delivery_type_labels.get(
                    delivery["delivery_type"], delivery["delivery_type"]
                ),
                "status": delivery["status"],
                "status_label": "已发送" if delivery["status"] == "sent" else "发送失败",
                "work_date": delivery["work_date"],
                "error_message": delivery["error_message"],
                "created_at": created_at.astimezone().strftime("%m月%d日 %H:%M"),
            })
        workspace_label = "当前仓库" if selected_source == "github" else "当前群聊"
        if selected_source == "github":
            workspace_name = selected_repository
            workspace_caption = "已连接 GitHub 仓库"
        elif feishu_needs_setup:
            workspace_name = "未选择群聊"
            workspace_caption = "请先在下方接入飞书群聊"
        else:
            workspace_name = session.get("selected_feishu_chat_name", "")
            workspace_caption = "已连接飞书群聊"
        return render_template(
            "dashboard.html",
            display_name=session["display_name"],
            today=today.isoformat(),
            today_label=f"{today.year}年{today.month}月{today.day}日 · 星期{('一', '二', '三', '四', '五', '六', '日')[today.weekday()]}",
            github_repository=workspace_name,
            workspace_label=workspace_label,
            workspace_display=workspace_name,
            workspace_caption=workspace_caption,
            commit_count=commit_count,
            feishu_message_count=feishu_message_count,
            progress_count=task_count + published_count,
            collaboration_count=collaboration_count,
            activity_items=activity_items,
            activity_count=activity_count,
            sync_status=sync_status,
            sender_email=sender_email,
            notification_email=notification_email,
            notification_email_2=notification_email_2,
            notification_email_3=notification_email_3,
            email_deliveries=email_deliveries,
            latest_email_delivery=email_deliveries[0] if email_deliveries else None,
            publish_only=publish_only,
            feishu_needs_setup=feishu_needs_setup,
            feishu_app_id=session.get("feishu_app_id") or "",
            feishu_chat_id=selected_feishu_chat_id or "",
            feishu_chat_name=session.get("selected_feishu_chat_name") or "",
            feishu_chat_options=session.get("feishu_chat_options") or [],
            active_page="workbench",
            page_title="工作台",
            breadcrumb_title="工作台",
            breadcrumb_subtitle="填写日报并同步发送",
            search_target=url_for("archive_page"),
        )

    @app.get("/archive")
    def archive_page():
        if "user_id" not in session:
            return redirect(url_for("login"))
        try:
            date_from, date_to = _date_window(
                request.args.get("from"),
                request.args.get("to"),
                default_days=30,
            )
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")

        query = request.args.get("q", "").strip().lower()
        page = _parse_page(request.args.get("page"))
        published_items = []
        github_items = []
        feishu_items = []

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            feishu_where_sql = (
                "WHERE work_date BETWEEN ? AND ?" if date_from and date_to else ""
            )
            feishu_parameters = (date_from, date_to) if date_from and date_to else ()
            if date_from and date_to:
                published_rows = connection.execute(
                    """SELECT p.id, p.work_date, p.title, p.content, p.report_detail,
                              p.attachments,
                              p.source_url, p.created_at, u.display_name AS member
                       FROM published_items p
                       JOIN publisher_users u ON u.id = p.publisher_id
                       WHERE p.work_date BETWEEN ? AND ?
                       ORDER BY p.work_date DESC, p.created_at DESC, p.id DESC""",
                    (date_from, date_to),
                ).fetchall()
                github_rows = connection.execute(
                    """SELECT id, repository, sha, work_date, message, additions,
                              deletions, changed_files, commit_url, committed_at_utc,
                              member_name AS member, synced_by_username
                       FROM github_commits
                       WHERE work_date BETWEEN ? AND ?
                       ORDER BY work_date DESC, committed_at_utc DESC, id DESC""",
                    (date_from, date_to),
                ).fetchall()
                feishu_rows = connection.execute(
                    f"""SELECT id, message_id, chat_id, chat_name, sender_id,
                               member_name, msg_type, work_date, summary,
                               created_at_utc, collected_at_utc
                        FROM feishu_messages
                        {feishu_where_sql}
                        ORDER BY work_date DESC, created_at_utc DESC, id DESC""",
                    feishu_parameters,
                ).fetchall()
            else:
                published_rows = connection.execute(
                    """SELECT p.id, p.work_date, p.title, p.content, p.report_detail,
                              p.attachments,
                              p.source_url, p.created_at, u.display_name AS member
                       FROM published_items p
                       JOIN publisher_users u ON u.id = p.publisher_id
                       ORDER BY p.work_date DESC, p.created_at DESC, p.id DESC"""
                ).fetchall()
                github_rows = connection.execute(
                    """SELECT id, repository, sha, work_date, message, additions,
                              deletions, changed_files, commit_url, committed_at_utc,
                              member_name AS member, synced_by_username
                       FROM github_commits
                       ORDER BY work_date DESC, committed_at_utc DESC, id DESC"""
                ).fetchall()
                feishu_rows = connection.execute(
                    """SELECT id, message_id, chat_id, chat_name, sender_id,
                              member_name, msg_type, work_date, summary,
                              created_at_utc, collected_at_utc
                       FROM feishu_messages
                       ORDER BY work_date DESC, created_at_utc DESC, id DESC"""
                ).fetchall()

        published_items = [_published_item(row) for row in published_rows]
        github_items = [_github_item(row) for row in github_rows]
        feishu_items = [_feishu_item(row) for row in feishu_rows]
        items = published_items + github_items + feishu_items
        items.sort(key=lambda item: (item["work_date"], item["created_at"]), reverse=True)

        if query:
            items = [
                item
                for item in items
                if query in " ".join([
                    item["title"],
                    item["content"],
                    item["member"],
                    item["repository"],
                    item.get("channel", ""),
                    item["source_type"],
                ]).lower()
            ]

        total_items = len(items)
        total_pages = max(1, math.ceil(total_items / ARCHIVE_PAGE_SIZE))
        page = min(page, total_pages)
        start_index = (page - 1) * ARCHIVE_PAGE_SIZE
        visible_items = items[start_index:start_index + ARCHIVE_PAGE_SIZE]
        page_window = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))

        return render_template(
            "archive.html",
            items=visible_items,
            item_count=total_items,
            page=page,
            total_pages=total_pages,
            page_window=page_window,
            date_from=date_from,
            date_to=date_to,
            query=query,
            today=datetime.date.today().isoformat(),
            last_7_days=(datetime.date.today() - datetime.timedelta(days=6)).isoformat(),
            active_page="archive",
            page_title="日报归档",
            breadcrumb_title="日报归档",
            breadcrumb_subtitle="团队工作记录",
            search_target=url_for("archive_page"),
        )

    @app.get("/github")
    def github_activity_page():
        if "user_id" not in session:
            return redirect(url_for("login"))
        try:
            date_from, date_to = _date_window(
                request.args.get("from"),
                request.args.get("to"),
                default_days=30,
            )
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")

        repository = request.args.get("repository", "").strip()
        if repository:
            try:
                repository = normalize_repository(repository)
            except GitHubSourceError as error:
                return Response(str(error), status=400, content_type="text/plain; charset=utf-8")
        else:
            repository = session.get("selected_repository", "")

        if repository:
            session["selected_source"] = "github"

        query = request.args.get("q", "").strip().lower()
        page = _parse_page(request.args.get("page"))
        conditions = []
        parameters = []
        if date_from and date_to:
            conditions.append("work_date BETWEEN ? AND ?")
            parameters.extend([date_from, date_to])
        if repository:
            conditions.append("repository = ?")
            parameters.append(repository)
        where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            repository_rows = connection.execute(
                "SELECT DISTINCT repository FROM github_commits ORDER BY repository"
            ).fetchall()
            notification_email_row = connection.execute(
                "SELECT notification_email FROM publisher_users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            rows = connection.execute(
                f"""SELECT id, repository, sha, work_date, message, additions,
                           deletions, changed_files, commit_url, committed_at_utc,
                           member_name AS member, synced_by_username
                    FROM github_commits
                    {where_sql}
                    ORDER BY committed_at_utc DESC, id DESC""",
                parameters,
            ).fetchall()
            sync_status = None
            if repository:
                sync_status = connection.execute(
                    """SELECT status, commit_count, completed_at_utc, error_message
                       FROM github_sync_runs
                       WHERE repository = ?
                       ORDER BY id DESC LIMIT 1""",
                    (repository,),
                ).fetchone()

        items = [_github_item(row) for row in rows]
        if query:
            items = [
                item
                for item in items
                if query in " ".join([
                    item["title"],
                    item["content"],
                    item["member"],
                    item["repository"],
                    item["sha_short"],
                ]).lower()
            ]

        total_items = len(items)
        total_pages = max(1, math.ceil(total_items / GITHUB_PAGE_SIZE))
        page = min(page, total_pages)
        start_index = (page - 1) * GITHUB_PAGE_SIZE
        visible_items = items[start_index:start_index + GITHUB_PAGE_SIZE]
        page_window = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
        stats = {
            "commit_count": total_items,
            "additions": sum(item["additions"] for item in items),
            "deletions": sum(item["deletions"] for item in items),
            "changed_files": sum(item["changed_files"] for item in items),
            "member_count": len({item["member"] for item in items}),
        }
        repository_options = [row["repository"] for row in repository_rows]

        return render_template(
            "github.html",
            items=visible_items,
            item_count=total_items,
            page=page,
            total_pages=total_pages,
            page_window=page_window,
            date_from=date_from,
            date_to=date_to,
            query=query,
            repository=repository,
            repository_options=repository_options,
            stats=stats,
            sync_status=sync_status,
            notification_email=(
                notification_email_row["notification_email"]
                if notification_email_row
                else ""
            ),
            active_page="github",
            page_title="GitHub 活动",
            breadcrumb_title="GitHub 活动",
            breadcrumb_subtitle="仓库活动流水",
            search_target=url_for("github_activity_page"),
        )

    @app.get("/feishu")
    def feishu_page():
        if "user_id" not in session:
            return redirect(url_for("login"))
        try:
            date_from, date_to = _date_window(
                request.args.get("from"),
                request.args.get("to"),
                default_days=30,
            )
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")

        query = request.args.get("q", "").strip().lower()
        page = _parse_page(request.args.get("page"))
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            user_row = connection.execute(
                """SELECT feishu_app_id, feishu_chat_id, feishu_chat_name,
                          notification_email
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()
            app_id = session.get("feishu_app_id") or (
                user_row["feishu_app_id"] if user_row else ""
            )
            chat_id = session.get("selected_feishu_chat_id") or (
                user_row["feishu_chat_id"] if user_row else ""
            )
            chat_name = session.get("selected_feishu_chat_name") or (
                user_row["feishu_chat_name"] if user_row else ""
            )
            if user_row:
                session["feishu_app_id"] = app_id
                session["selected_feishu_chat_id"] = chat_id
                session["selected_feishu_chat_name"] = chat_name
            if chat_id:
                session["selected_source"] = "feishu"

            conditions = []
            parameters = []
            if date_from and date_to:
                conditions.append("work_date BETWEEN ? AND ?")
                parameters.extend([date_from, date_to])
            if chat_id:
                conditions.append("chat_id = ?")
                parameters.append(chat_id)
            where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
            rows = connection.execute(
                f"""SELECT id, message_id, chat_id, chat_name, sender_id,
                           member_name, msg_type, work_date, summary,
                           created_at_utc, collected_at_utc
                    FROM feishu_messages
                    {where_sql}
                    ORDER BY created_at_utc DESC, id DESC""",
                parameters,
            ).fetchall()
            sync_status = None
            if chat_id:
                sync_status = connection.execute(
                    """SELECT status, message_count, completed_at_utc, error_message
                       FROM feishu_sync_runs
                       WHERE chat_id = ?
                       ORDER BY id DESC LIMIT 1""",
                    (chat_id,),
                ).fetchone()

        chat_options = session.get("feishu_chat_options") or []
        if chat_id and chat_name and not any(
            option.get("chat_id") == chat_id for option in chat_options
        ):
            chat_options = [
                *chat_options,
                {"chat_id": chat_id, "chat_name": chat_name, "description": ""},
            ]

        items = [_feishu_item(row) for row in rows]
        if query:
            items = [
                item
                for item in items
                if query in " ".join([
                    item["title"],
                    item["content"],
                    item["member"],
                    item["channel"],
                    item["message_type"],
                ]).lower()
            ]

        total_items = len(items)
        total_pages = max(1, math.ceil(total_items / FEISHU_PAGE_SIZE))
        page = min(page, total_pages)
        start_index = (page - 1) * FEISHU_PAGE_SIZE
        visible_items = items[start_index:start_index + FEISHU_PAGE_SIZE]
        page_window = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
        stats = {
            "message_count": len(rows),
            "member_count": len({row["member_name"] for row in rows}),
            "type_count": len({row["msg_type"] for row in rows}),
            "today_count": sum(
                row["work_date"] == datetime.date.today().isoformat() for row in rows
            ),
        }

        return render_template(
            "feishu.html",
            items=visible_items,
            item_count=total_items,
            page=page,
            total_pages=total_pages,
            page_window=page_window,
            date_from=date_from,
            date_to=date_to,
            query=query,
            app_id=app_id,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_options=chat_options,
            configured=bool(app_id and chat_id),
            stats=stats,
            sync_status=sync_status,
            notification_email=(
                user_row["notification_email"] if user_row else ""
            ),
            active_page="feishu",
            page_title="飞书群聊",
            breadcrumb_title="飞书群聊",
            breadcrumb_subtitle="群聊消息流水",
            search_target=url_for("feishu_page"),
        )

    @app.post("/feishu/settings")
    def save_feishu_settings():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home", _anchor="activity-stream"))

        action = request.form.get("action", "connect")
        entered_app_id = request.form.get("app_id", "").strip()
        app_secret = request.form.get("app_secret", "")
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            user_row = connection.execute(
                """SELECT feishu_app_id, feishu_chat_id, feishu_chat_name
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()
        current_app_id = (
            session.get("feishu_app_id")
            or (user_row["feishu_app_id"] if user_row else "")
        )

        if action == "select":
            chat_id = request.form.get("chat_id", "").strip()
            current_chat_id = (
                session.get("selected_feishu_chat_id")
                or (user_row["feishu_chat_id"] if user_row else "")
            )
            known_chats = session.get("feishu_chat_options") or []
            if not known_chats and chat_id and chat_id == current_chat_id:
                current_chat_name = (
                    session.get("selected_feishu_chat_name")
                    or (user_row["feishu_chat_name"] if user_row else "")
                )
                known_chats = [
                    {"chat_id": chat_id, "chat_name": current_chat_name}
                ]
            selected_chat = next(
                (chat for chat in known_chats if chat.get("chat_id") == chat_id),
                None,
            )
            if selected_chat is None:
                flash("请先获取并选择有效的飞书群聊。", "error")
                return redirect(url_for("home"))

            app_id = entered_app_id or current_app_id
            chat_name = selected_chat.get("chat_name", "") or "未命名群聊"
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                connection.execute(
                    """UPDATE publisher_users
                       SET feishu_app_id = ?, feishu_chat_id = ?, feishu_chat_name = ?
                       WHERE id = ?""",
                    (app_id, chat_id, chat_name, session["user_id"]),
                )
            session["feishu_app_id"] = app_id
            session["selected_feishu_chat_id"] = chat_id
            session["selected_feishu_chat_name"] = chat_name
            session["selected_source"] = "feishu"
            flash(f"已接入飞书群聊「{chat_name}」，工作台将展示该群聊协作记录。", "success")
            return redirect(url_for("home"))

        if not entered_app_id:
            flash("飞书 App ID 不能为空。", "error")
            return redirect(url_for("feishu_page"))
        try:
            if app_secret:
                store_feishu_app_secret(entered_app_id, app_secret)
            elif entered_app_id != current_app_id:
                raise FeishuSourceError("更换 App ID 时请重新填写 App Secret。")
            chats = list_feishu_chats(
                entered_app_id,
                app_secret or None,
            )
        except FeishuSourceError as error:
            flash(f"飞书接口验证失败：{error}", "error")
            return redirect(url_for("home"))

        current_chat_id = session.get("selected_feishu_chat_id", "")
        selected_chat = next(
            (chat for chat in chats if chat.get("chat_id") == current_chat_id),
            None,
        )
        session["feishu_app_id"] = entered_app_id
        session["feishu_chat_options"] = chats
        session["selected_feishu_chat_id"] = selected_chat["chat_id"] if selected_chat else ""
        session["selected_feishu_chat_name"] = selected_chat["chat_name"] if selected_chat else ""
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            connection.execute(
                """UPDATE publisher_users
                   SET feishu_app_id = ?, feishu_chat_id = ?, feishu_chat_name = ?
                   WHERE id = ?""",
                (
                    entered_app_id,
                    selected_chat["chat_id"] if selected_chat else "",
                    selected_chat["chat_name"] if selected_chat else "",
                    session["user_id"],
                ),
            )
        if chats:
            flash("飞书接口验证成功，请选择要接入的群聊。", "success")
        else:
            flash("飞书接口验证成功，但当前应用可见的群聊为空。请先把应用加入群聊。", "warning")
        return redirect(url_for("home"))

    @app.post("/feishu/sync")
    def sync_feishu():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("feishu_page"))

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            user_row = connection.execute(
                """SELECT feishu_app_id, feishu_chat_id, feishu_chat_name
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()
        chat_id = (
            session.get("selected_feishu_chat_id")
            or (user_row["feishu_chat_id"] if user_row else "")
        )
        app_id = (
            session.get("feishu_app_id")
            or (user_row["feishu_app_id"] if user_row else "")
        )
        chat_name = (
            session.get("selected_feishu_chat_name")
            or (user_row["feishu_chat_name"] if user_row else "")
        )
        if not chat_id:
            flash("请先选择要同步的飞书群聊。", "error")
            return redirect(url_for("home", _anchor="activity-stream"))

        started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

        try:
            chat_name, messages = collect_feishu_recent_messages(
                chat_id,
                app_id=app_id or None,
                chat_name=chat_name,
            )
            completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            session["selected_feishu_chat_name"] = chat_name
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                new_count = persist_feishu_messages(
                    connection,
                    messages,
                    chat_id,
                    chat_name,
                    started,
                    completed,
                    user_id=session["user_id"],
                    actor_name=session.get("display_name"),
                )

            flash(
                f"飞书同步完成：最近 5 分钟获取 {len(messages)} 条消息，新增 {new_count} 条协作记录。",
                "success",
            )
        except FeishuSourceError as error:
            completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            error_message = str(error)[:240]
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                connection.execute(
                    """INSERT INTO feishu_sync_runs
                       (chat_id, chat_name, status, message_count, started_at_utc,
                        completed_at_utc, error_message)
                       VALUES (?, ?, 'failed', 0, ?, ?, ?)""",
                    (chat_id, chat_name or chat_id, started, completed, error_message),
                )
                log_audit(
                    connection,
                    "feishu_sync_failed",
                    "feishu_chat",
                    actor_id=session["user_id"],
                    actor_name=session["display_name"],
                    summary={"error_code": "source_unavailable"},
                )
            flash(f"飞书同步失败：{error}", "error")
        return redirect(url_for("home", _anchor="activity-stream"))

    @app.post("/feishu/send")
    def send_feishu_messages():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("feishu_page"))

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            user_row = connection.execute(
                """SELECT feishu_chat_id, feishu_chat_name,
                          sender_email, notification_email,
                          notification_email_2, notification_email_3
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()
        chat_id = (
            session.get("selected_feishu_chat_id")
            or (user_row["feishu_chat_id"] if user_row else "")
        )
        chat_name = (
            session.get("selected_feishu_chat_name")
            or (user_row["feishu_chat_name"] if user_row else "")
        )
        sender_email = (
            user_row["sender_email"] if user_row else ""
        ) or local_sender_email()
        recipients = _normalize_recipients(
            user_row["notification_email"] if user_row else "",
            user_row["notification_email_2"] if user_row else "",
            user_row["notification_email_3"] if user_row else "",
        )

        if not chat_id:
            flash("请先选择要发送的飞书群聊。", "error")
            return redirect(url_for("feishu_page"))
        if not recipients:
            flash("请先在工作台填写收件人邮箱。", "error")
            return redirect(url_for("feishu_page"))

        draft = session.get("progress_draft") or {}
        work_date = draft.get("work_date", "") or datetime.date.today().isoformat()

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            stream_label, stream_items = _stream_for_publish(
                connection, "feishu", chat_id, None, work_date
            )

        if not stream_items:
            flash("当前群聊还没有同步到消息，请先同步。", "error")
            return redirect(url_for("feishu_page"))

        sent, failed = deliver_progress_email(
            sender_email,
            recipients,
            "feishu_sync",
            work_date,
            draft.get("finished", ""),
            draft.get("problems", ""),
            draft.get("next_steps", ""),
            status=draft.get("status", "已完成"),
            source_url=draft.get("source_url", ""),
            stream_items=stream_items,
            stream_label=stream_label,
        )
        if failed:
            flash(
                "发送失败："
                + "；".join(f"{address}：{error}" for address, error in failed),
                "error",
            )
        else:
            flash(
                f"已将工作台信息与 {len(stream_items)} 条群聊消息发送到 "
                + "、".join(sent)
                + "。",
                "success",
            )
        return redirect(url_for("feishu_page"))

    @app.route("/repository", methods=["GET", "POST"])
    def select_repository():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if request.method == "POST":
            if not csrf_valid():
                flash("页面已过期，请刷新后重试。", "error")
                return render_template(
                    "repository.html",
                    current_repository=session.get("selected_repository", ""),
                ), 400
            entered_repository = request.form.get("repository", "").strip()
            try:
                selected_repository = validate_repository(entered_repository)
            except GitHubSourceError as error:
                flash(f"仓库不可用：{error}", "error")
                return render_template(
                    "repository.html",
                    current_repository=entered_repository,
                ), 400
            session["selected_repository"] = selected_repository
            session["selected_source"] = "github"
            return redirect(url_for("home", publish_only=1))
        return render_template(
            "repository.html",
            current_repository=session.get("selected_repository", ""),
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if not csrf_valid():
                flash("页面已过期，请刷新后重试。", "error")
                return render_template("login.html"), 400
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                user = connection.execute(
                    """SELECT id, username, password_hash, display_name,
                           sender_email, notification_email,
                           notification_email_2, notification_email_3,
                           feishu_app_id, feishu_chat_id, feishu_chat_name
                       FROM publisher_users WHERE username = ? AND enabled = 1""",
                    (username,),
                ).fetchone()
            if not user or not verify_password(password, user["password_hash"] if user else ""):
                with database_connection(app.config["DATABASE_PATH"]) as connection:
                    log_audit(
                        connection,
                        "login_failed",
                        "session",
                        actor_name="未知用户",
                    )
                flash("用户名或密码错误。", "error")
                return render_template("login.html"), 401
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            session["sender_email"] = user["sender_email"]
            session["notification_email"] = user["notification_email"]
            session["notification_email_2"] = user["notification_email_2"]
            session["notification_email_3"] = user["notification_email_3"]
            session["feishu_app_id"] = user["feishu_app_id"]
            session["selected_feishu_chat_id"] = user["feishu_chat_id"]
            session["selected_feishu_chat_name"] = user["feishu_chat_name"]
            csrf_token()
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                log_audit(
                    connection,
                    "login_succeeded",
                    "session",
                    actor_id=user["id"],
                    actor_name=user["display_name"],
                )
                connection.execute("DELETE FROM feishu_messages")
                connection.execute("DELETE FROM github_commits")
                connection.execute("DELETE FROM feishu_sync_runs")
                connection.execute("DELETE FROM github_sync_runs")
                connection.execute(
                    "DELETE FROM records WHERE category = 'collaborations' AND extra_json LIKE ?",
                    ('%"source_type": "feishu"%',),
                )
            return redirect(url_for("select_source"))
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if "user_id" in session:
            return redirect(url_for("home"))
        if request.method == "POST":
            if not csrf_valid():
                flash("页面已过期，请刷新后重试。", "error")
                return render_template("register.html"), 400

            username = request.form.get("username", "").strip().lower()
            display_name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")
            error = None
            if not re.fullmatch(r"[a-z0-9_.-]{3,32}", username):
                error = "用户名需为 3–32 位字母、数字或 . _ -。"
            elif not display_name or len(display_name) > 80:
                error = "显示名称不能为空且不能超过 80 个字符。"
            elif not 12 <= len(password) <= 128:
                error = "密码长度需为 12–128 个字符。"
            elif password != password_confirm:
                error = "两次输入的密码不一致。"

            if error:
                flash(error, "error")
                return render_template("register.html"), 400

            password_hash = hash_password(password)
            try:
                with database_connection(app.config["DATABASE_PATH"]) as connection:
                    duplicate = connection.execute(
                        "SELECT 1 FROM publisher_users WHERE lower(username) = ?",
                        (username,),
                    ).fetchone()
                    if duplicate:
                        flash("该用户名已被使用，请换一个用户名。", "error")
                        return render_template("register.html"), 409
                    cursor = connection.execute(
                        """INSERT INTO publisher_users (username, password_hash, display_name)
                           VALUES (?, ?, ?)""",
                        (username, password_hash, display_name),
                    )
                    user_id = cursor.lastrowid
                    log_audit(
                        connection,
                        "account_registered",
                        "publisher_user",
                        actor_id=user_id,
                        actor_name=display_name,
                    )
            except sqlite3.IntegrityError:
                flash("该用户名已被使用，请换一个用户名。", "error")
                return render_template("register.html"), 409

            session.clear()
            session["user_id"] = user_id
            session["username"] = username
            session["display_name"] = display_name
            session["sender_email"] = ""
            session["notification_email"] = ""
            session["notification_email_2"] = ""
            session["notification_email_3"] = ""
            session["feishu_app_id"] = ""
            session["selected_feishu_chat_id"] = ""
            session["selected_feishu_chat_name"] = ""
            csrf_token()
            flash("账号创建成功，已自动登录。", "success")
            return redirect(url_for("select_source"))
        return render_template("register.html")

    @app.post("/logout")
    def logout():
        if not csrf_valid():
            return Response("页面已过期，请刷新后重试。", status=400, content_type="text/plain; charset=utf-8")
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            log_audit(
                connection,
                "logout",
                "session",
                actor_id=session["user_id"],
                actor_name=session["display_name"],
            )
        session.clear()
        return redirect(url_for("login"))

    @app.post("/email/settings")
    def save_email_settings():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home", _anchor="daily-form"))
        sender_email = (
            request.form.get("sender_email", "").strip()
            or local_sender_email()
        )
        recipient_email = request.form.get(
            "recipient_email",
            request.form.get("notification_email", ""),
        ).strip()
        recipient_email_2 = request.form.get("recipient_email_2", "").strip()
        recipient_email_3 = request.form.get("recipient_email_3", "").strip()
        smtp_password = request.form.get("smtp_password", "")
        try:
            save_email_channel(
                sender_email,
                recipient_email,
                smtp_password,
                recipient_email_2,
                recipient_email_3,
            )
            smtp_settings_from_env(sender_email)
        except EmailNotificationError as error:
            flash(str(error), "warning" if "授权码" in str(error) else "error")
        else:
            flash("邮箱配置已保存，任务进展、GitHub 与飞书同步都会使用这个收发通道。", "success")
        return redirect(url_for("home", _anchor="daily-form"))

    @app.post("/email/test")
    def test_email_settings():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home", _anchor="daily-form"))
        sender_email = (
            request.form.get("sender_email", "").strip()
            or local_sender_email()
        )
        recipient_email = request.form.get(
            "recipient_email",
            request.form.get("notification_email", ""),
        ).strip()
        recipient_email_2 = request.form.get("recipient_email_2", "").strip()
        recipient_email_3 = request.form.get("recipient_email_3", "").strip()
        smtp_password = request.form.get("smtp_password", "")
        try:
            save_email_channel(
                sender_email,
                recipient_email,
                smtp_password,
                recipient_email_2,
                recipient_email_3,
            )
            send_test_email(smtp_settings_from_env(sender_email), recipient_email)
        except EmailNotificationError as error:
            flash(f"测试邮件未发送：{error}", "error")
        else:
            flash(f"测试邮件已从 {sender_email} 发送到 {recipient_email}。", "success")
        return redirect(url_for("home", _anchor="daily-form"))

    @app.post("/save-progress-draft")
    def save_progress_draft():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home", _anchor="daily-form"))
        session["progress_draft"] = {
            "work_date": request.form.get("work_date", "").strip(),
            "status": request.form.get("status", "已完成").strip() or "已完成",
            "source_url": request.form.get("source_url", "").strip(),
            "finished": request.form.get("finished", "").strip(),
            "problems": request.form.get("problems", "").strip(),
            "next_steps": request.form.get("next_steps", "").strip(),
        }
        sender_email = (
            request.form.get("sender_email", "").strip() or local_sender_email()
        )
        recipient_email = request.form.get(
            "recipient_email",
            request.form.get("notification_email", ""),
        ).strip()
        recipient_email_2 = request.form.get("recipient_email_2", "").strip()
        recipient_email_3 = request.form.get("recipient_email_3", "").strip()
        smtp_password = request.form.get("smtp_password", "")
        try:
            save_email_channel(
                sender_email,
                recipient_email,
                smtp_password,
                recipient_email_2,
                recipient_email_3,
            )
        except EmailNotificationError as error:
            flash(f"工作台信息已保存，但邮箱配置未保存：{error}", "warning")
        else:
            flash("工作台信息与邮箱配置已保存，前往飞书群聊或 GitHub 页面发布流水时会一并发送。", "success")
        return redirect(url_for("home", _anchor="daily-form"))

    @app.post("/publish")
    def publish():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home"))
        sender_email = (
            request.form.get("sender_email", "").strip()
            or local_sender_email()
        )
        notification_email = request.form.get(
            "recipient_email",
            request.form.get("notification_email", ""),
        ).strip()
        recipient_email_2 = request.form.get("recipient_email_2", "").strip()
        recipient_email_3 = request.form.get("recipient_email_3", "").strip()
        smtp_password = request.form.get("smtp_password", "")
        publish_action = request.form.get("publish_action", "save_send").strip()
        report_detail = request.form.get("report_detail", "标准").strip()
        if report_detail not in {"简洁", "标准", "详细"}:
            report_detail = "标准"
        try:
            recipients = save_email_channel(
                sender_email,
                notification_email,
                smtp_password,
                recipient_email_2,
                recipient_email_3,
            )
        except EmailNotificationError as error:
            flash(str(error), "error")
            return redirect(url_for("home", _anchor="daily-form"))
        finished = request.form.get("finished", "").strip()
        problems = request.form.get("problems", "").strip()
        next_steps = request.form.get("next_steps", "").strip()
        work_status = request.form.get("status", "已完成").strip() or "已完成"
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        progress_sections = (
            ("完成事项", finished),
            ("问题与风险", problems),
            ("下一步计划", next_steps),
        )
        if any(value for _, value in progress_sections):
            title = title or "今日工作进展"
            content = (
                f"工作状态：{work_status}\n\n"
                + "\n\n".join(
                f"{label}：\n{value}"
                for label, value in progress_sections
                if value
                )
            )
        source_url = request.form.get("source_url", "").strip()
        work_date = request.form.get("work_date", "")
        try:
            parsed_date = datetime.date.fromisoformat(work_date)
            if parsed_date.isoformat() != work_date:
                raise ValueError
        except ValueError:
            flash("请选择有效的工作日期。", "error")
            return redirect(url_for("home"))
        if not title or not content:
            flash("标题和内容不能为空。", "error")
            return redirect(url_for("home"))
        if len(title) > 200:
            flash("标题不能超过 200 个字符。", "error")
            return redirect(url_for("home"))
        if source_url and not source_url.startswith(("https://", "http://")):
            flash("链接必须以 http:// 或 https:// 开头。", "error")
            return redirect(url_for("home"))
        quality_messages = quality_warnings(
            {
                "finished": finished,
                "problems": problems,
                "next_steps": next_steps,
            }
        )
        if quality_messages:
            flash("日报质量提示：" + " ".join(quality_messages), "warning")
        attachment_paths = []
        try:
            attachment_paths = _save_uploaded_attachments(
                request.files.getlist("attachments"),
                app.config["UPLOAD_FOLDER"],
            )
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("home", _anchor="daily-form"))
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            cursor = connection.execute(
                """INSERT INTO published_items
                   (publisher_id, work_date, title, content, report_detail,
                    attachments, source_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session["user_id"],
                    work_date,
                    title,
                    content,
                    report_detail,
                    json.dumps(attachment_paths, ensure_ascii=False),
                    source_url or None,
                    created_at,
                ),
            )
            log_audit(
                connection,
                "published_item_created",
                "published_item",
                actor_id=session["user_id"],
                actor_name=session["display_name"],
                target_id=cursor.lastrowid,
                work_date=work_date,
                summary={"has_source_url": bool(source_url)},
            )
        should_send = publish_action == "save_send" and bool(recipients)
        if should_send:
            stream_label = None
            stream_items = []
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                stream_label, stream_items = _stream_for_publish(
                    connection,
                    session.get("selected_source"),
                    session.get("selected_feishu_chat_id"),
                    session.get("selected_repository"),
                    work_date,
                )
            sent, failed = deliver_progress_email(
                sender_email,
                recipients,
                "daily_report",
                work_date,
                finished,
                problems,
                next_steps,
                status=work_status,
                source_url=source_url,
                stream_items=stream_items,
                stream_label=stream_label,
                report_detail=report_detail,
                attachments=[
                    str(Path(app.config["UPLOAD_FOLDER"]) / path)
                    for path in attachment_paths
                ],
            )
            if failed:
                flash(
                    "发布成功，但任务完成邮件未发送到 "
                    + "、".join(address for address, _ in failed)
                    + "。",
                    "warning",
                )
            else:
                flash(
                    "发布成功，日报已保存到归档并发送到 "
                    + "、".join(sent)
                    + "。",
                    "success",
                )
        elif publish_action == "save_send":
            flash("发布成功，日报已保存到归档；未发送邮件，请填写目的邮箱。", "warning")
        else:
            flash("发布成功，信息已自动归档到所选日期。", "success")
        return redirect(url_for("home", _anchor="daily-form"))

    @app.post("/github/sync")
    def sync_github():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home"))
        selected_repository = session.get("selected_repository")
        if not selected_repository:
            return redirect(url_for("select_repository"))

        started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        repository = selected_repository
        actor_id = session["user_id"]
        actor_name = session["display_name"]
        actor_username = session["username"]
        try:
            repository, commits = collect_last_24_hours(repository=selected_repository)
            completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                for commit in commits:
                    connection.execute(
                        """INSERT INTO github_commits (
                            repository, sha, github_login, synced_by_username, member_name, work_date, message,
                            additions, deletions, changed_files, commit_url,
                            committed_at_utc, collected_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(repository, sha) DO UPDATE SET
                            github_login=excluded.github_login,
                            synced_by_username=excluded.synced_by_username,
                            member_name=excluded.member_name,
                            work_date=excluded.work_date,
                            message=excluded.message,
                            additions=excluded.additions,
                            deletions=excluded.deletions,
                            changed_files=excluded.changed_files,
                            commit_url=excluded.commit_url,
                            committed_at_utc=excluded.committed_at_utc,
                            collected_at_utc=excluded.collected_at_utc""",
                        (commit["repository"], commit["sha"], commit["github_login"],
                         actor_username, commit["member_name"], commit["work_date"], commit["message"],
                         commit["additions"], commit["deletions"], commit["changed_files"],
                         commit["commit_url"], commit["committed_at_utc"], completed),
                    )
                connection.execute(
                    """INSERT INTO github_sync_runs
                       (repository, status, commit_count, started_at_utc, completed_at_utc)
                       VALUES (?, 'succeeded', ?, ?, ?)""",
                    (repository, len(commits), started, completed),
                )
                log_audit(connection, "github_sync_succeeded", "github_repository",
                          actor_id=actor_id, actor_name=actor_name,
                          work_date=datetime.date.today().isoformat(),
                          summary={"commit_count": len(commits)})
            flash(f"GitHub 提取完成：最近 24 小时获取 {len(commits)} 条提交。", "success")
        except GitHubSourceError as error:
            completed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            error_message = str(error)[:240]
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                connection.execute(
                    """INSERT INTO github_sync_runs
                       (repository, status, commit_count, started_at_utc, completed_at_utc, error_message)
                       VALUES (?, 'failed', 0, ?, ?, ?)""",
                    (repository, started, completed, error_message),
                )
                log_audit(connection, "github_sync_failed", "github_repository",
                          actor_id=actor_id, actor_name=actor_name,
                          summary={"error_code": "source_unavailable"})
            flash(f"GitHub 提取失败：{error}", "error")
        return redirect(url_for("home", _anchor="activity-stream"))

    @app.post("/github/send")
    def send_github_commits():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("github_activity_page"))

        selected_repository = session.get("selected_repository")
        if not selected_repository:
            flash("请先选择 GitHub 仓库。", "error")
            return redirect(url_for("select_repository"))

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            user_row = connection.execute(
                """SELECT sender_email, notification_email,
                          notification_email_2, notification_email_3
                   FROM publisher_users WHERE id = ?""",
                (session["user_id"],),
            ).fetchone()
        sender_email = (
            user_row["sender_email"] if user_row else ""
        ) or local_sender_email()
        recipients = _normalize_recipients(
            user_row["notification_email"] if user_row else "",
            user_row["notification_email_2"] if user_row else "",
            user_row["notification_email_3"] if user_row else "",
        )

        if not recipients:
            flash("请先在工作台填写收件人邮箱。", "error")
            return redirect(url_for("github_activity_page"))

        draft = session.get("progress_draft") or {}
        work_date = draft.get("work_date", "") or datetime.date.today().isoformat()

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            stream_label, stream_items = _stream_for_publish(
                connection, "github", None, selected_repository, work_date
            )

        if not stream_items:
            flash("当前仓库还没有同步到提交记录，请先同步。", "error")
            return redirect(url_for("github_activity_page"))

        sent, failed = deliver_progress_email(
            sender_email,
            recipients,
            "github_sync",
            work_date,
            draft.get("finished", ""),
            draft.get("problems", ""),
            draft.get("next_steps", ""),
            status=draft.get("status", "已完成"),
            source_url=draft.get("source_url", ""),
            stream_items=stream_items,
            stream_label=stream_label,
        )
        if failed:
            flash(
                "发送失败："
                + "；".join(f"{address}：{error}" for address, error in failed),
                "error",
            )
        else:
            flash(
                f"已将工作台信息与 {len(stream_items)} 条提交记录发送到 "
                + "、".join(sent)
                + "。",
                "success",
            )
        return redirect(url_for("github_activity_page"))

    @app.get("/notifications")
    def notifications_page():
        return redirect(url_for("github_activity_page"))

    @app.post("/notifications/<int:notification_id>/read")
    def mark_notification_read(notification_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            return Response("页面已过期，请刷新后重试。", status=400, content_type="text/plain; charset=utf-8")
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            connection.execute(
                """UPDATE user_notifications
                   SET read_at = COALESCE(read_at, ?)
                   WHERE id = ? AND user_id = ?""",
                (
                    datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    notification_id,
                    session["user_id"],
                ),
            )
        next_url = request.form.get("next", "")
        if not next_url.startswith("/"):
            next_url = url_for("home")
        return redirect(next_url)

    @app.post("/notifications/read-all")
    def mark_all_notifications_read():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not csrf_valid():
            return Response("页面已过期，请刷新后重试。", status=400, content_type="text/plain; charset=utf-8")
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            connection.execute(
                """UPDATE user_notifications
                   SET read_at = ?
                   WHERE user_id = ? AND read_at IS NULL""",
                (
                    datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    session["user_id"],
                ),
            )
        next_url = request.form.get("next", "")
        if not next_url.startswith("/"):
            next_url = url_for("home")
        return redirect(next_url)

    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="启动本机信息发布系统")
    parser.add_argument("--port", type=int, default=5001, help="服务端口，默认 5001")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        initialize_database(DATABASE_PATH)
    except Exception as error:
        print(f"错误：数据库初始化失败（{type(error).__name__}）。", file=sys.stderr)
        return 1
    start_feishu_auto_sync(DATABASE_PATH)
    try:
        create_publisher_app().run(host="127.0.0.1", port=args.port, debug=False)
    finally:
        stop_feishu_auto_sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
