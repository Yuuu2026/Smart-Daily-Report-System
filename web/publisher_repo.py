"""发布端的数据层：数据库读写与「记录行 -> 视图模型」转换。

这里不放任何 Flask 路由，只做三件事：
1. 把飞书消息、GitHub 提交、成员日报写进 SQLite（幂等）；
2. 读出提交/消息流水，供发布时生成 Markdown；
3. 把数据库行转成模板友好的 dict，并统一时间显示口径。
"""

import datetime
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

from report_core import parse_progress_sections
from web.app import database_connection
from web.audit import log_audit
from web.email_notifications import EmailNotificationError, is_valid_email
from web.feishu_source import FeishuSourceError, collect_feishu_recent_messages

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_ATTACHMENT_COUNT = 6
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
FEISHU_AUTO_SYNC_INTERVAL_SECONDS = 5
STREAM_LIMIT = 80  # 发布时最多带上的流水条数


# --------------------------------------------------------------------------
# 时间与参数解析
# --------------------------------------------------------------------------


def utc_now_iso():
    """当前 UTC 时间，秒级 ISO 字符串（数据库统一存这个格式）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def to_local(value):
    """把库里的 UTC 字符串转成本地时间；返回 (UTC datetime, 本地 datetime)。"""
    moment = datetime.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment, moment.astimezone()


def local_label(value, template="%m月%d日 %H:%M"):
    """把时间戳格式化成「10月21日 13:05」这类本地显示文本。"""
    return to_local(value)[1].strftime(template)


def relative_time(value):
    """把时间戳说成「3 分钟前 / 2 小时前 / 昨天 / 10月20日」。"""
    moment, _ = to_local(value)
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed = max(0, int((now - moment).total_seconds()))
    if elapsed < 3600:
        return f"{max(1, elapsed // 60)} 分钟前"
    if elapsed < 86400:
        return f"{elapsed // 3600} 小时前"
    if elapsed < 172800:
        return "昨天"
    return moment.astimezone().strftime("%m月%d日")


def parse_date(value):
    """严格解析 YYYY-MM-DD；空值返回 None，非法格式抛 ValueError。"""
    if not value:
        return None
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期必须是有效的 YYYY-MM-DD 日期。") from None
    if parsed.isoformat() != value:
        raise ValueError("日期必须是 YYYY-MM-DD 格式。")
    return parsed


def date_window(from_value, to_value, default_days=30):
    """把可选的起止日期补全为 (start, end) ISO 字符串。"""
    if not from_value and not to_value:
        return "", ""
    today = datetime.date.today()
    start = parse_date(from_value) or today - datetime.timedelta(days=default_days)
    end = parse_date(to_value) or today
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    return start.isoformat(), end.isoformat()


def parse_page(value):
    """页码一律当作从 1 开始的正整数，非法输入回落到第 1 页。"""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


# --------------------------------------------------------------------------
# 视图模型：数据库行 -> 模板 dict
# --------------------------------------------------------------------------


def _base_item(row, prefix, time_field, member, source_type):
    """三类记录共有的字段，避免每个构造函数重复拼一遍。"""
    _, local_moment = to_local(row[time_field])
    return {
        "id": f"{prefix}-{row['id']}",
        "work_date": row["work_date"],
        "created_at": row[time_field],
        "local_created_at": local_moment.strftime("%Y-%m-%d %H:%M"),
        "relative_time": relative_time(row[time_field]),
        "member": member,
        "source_type": source_type,
        "repository": "",
        "sha_short": "",
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
    }


def published_item(row):
    """成员自己发布的日报。"""
    content = (row["content"] or "").strip()
    sections = parse_progress_sections(content)
    labels = (
        ("finished", "完成事项"),
        ("problems", "问题与风险"),
        ("next_steps", "下一步计划"),
    )
    item = _base_item(row, "publish", "created_at", row["member"], "成员日报")
    item.update(
        title=row["title"].strip() or "今日工作进展",
        content=content,
        status=sections["status"] or "未标记状态",
        report_detail=(
            row["report_detail"] if "report_detail" in row.keys() else "标准"
        ),
        attachments=attachment_urls(
            row["attachments"] if "attachments" in row.keys() else "[]"
        ),
        sections=[
            {"label": label, "text": sections[field]}
            for field, label in labels
            if sections[field]
        ],
        source_url=row["source_url"],
    )
    return item


def github_item(row):
    """GitHub 提交。"""
    message = (row["message"] or "").strip()
    item = _base_item(row, "github", "committed_at_utc", row["member"], "GitHub 提交")
    item.update(
        title=message.splitlines()[0] if message else "无提交说明",
        content=message,
        source_url=row["commit_url"],
        repository=row["repository"],
        sha_short=(row["sha"] or "")[:7],
        additions=row["additions"],
        deletions=row["deletions"],
        changed_files=row["changed_files"],
    )
    return item


def feishu_item(row):
    """飞书群聊消息。"""
    summary = (row["summary"] or "").strip()
    item = _base_item(row, "feishu", "created_at_utc", row["member_name"], "飞书群聊")
    item.update(
        title=" ".join(summary.split())[:80] or "无消息内容",
        content=summary,
        source_url="",
        status=row["msg_type"] or "消息",
        channel=row["chat_name"],
        message_type=row["msg_type"] or "消息",
    )
    return item


def attachment_urls(raw_attachments):
    """把库里存的 JSON 附件路径转成可访问的 /uploads/... URL。"""
    try:
        attachments = json.loads(raw_attachments or "[]")
    except (TypeError, ValueError):
        attachments = []
    if not isinstance(attachments, list):
        return []
    return [
        f"/uploads/{str(item).lstrip('/')}" for item in attachments if str(item).strip()
    ]


# --------------------------------------------------------------------------
# 首页「最近动态」：列表 + 总数 + 最近一次同步状态
# --------------------------------------------------------------------------

_ACTIVITY_SOURCES = {
    "github": {
        "list": """SELECT id, repository, sha, work_date, message, additions, deletions,
                          changed_files, commit_url, committed_at_utc,
                          member_name AS member
                   FROM github_commits WHERE repository = ?
                   ORDER BY committed_at_utc DESC, id DESC LIMIT ?""",
        "count": "SELECT COUNT(*) FROM github_commits WHERE repository = ?",
        "sync": """SELECT status, commit_count, completed_at_utc, error_message
                   FROM github_sync_runs WHERE repository = ?
                   ORDER BY id DESC LIMIT 1""",
        "build": github_item,
    },
    "feishu": {
        "list": """SELECT id, message_id, chat_id, chat_name, sender_id, member_name,
                          msg_type, work_date, summary, created_at_utc, collected_at_utc
                   FROM feishu_messages WHERE chat_id = ?
                   ORDER BY created_at_utc DESC, id DESC LIMIT ?""",
        "count": "SELECT COUNT(*) FROM feishu_messages WHERE chat_id = ?",
        "sync": """SELECT status, message_count, completed_at_utc, error_message
                   FROM feishu_sync_runs WHERE chat_id = ?
                   ORDER BY id DESC LIMIT 1""",
        "build": feishu_item,
    },
}


def activity_snapshot(connection, source, identifier, limit=12):
    """返回 {items, count, sync}；数据源未知或未选中时全部为空。"""
    spec = _ACTIVITY_SOURCES.get(source)
    if not spec or not identifier:
        return {"items": [], "count": 0, "sync": None}
    rows = connection.execute(spec["list"], (identifier, limit)).fetchall()
    count = connection.execute(spec["count"], (identifier,)).fetchone()[0]
    sync = connection.execute(spec["sync"], (identifier,)).fetchone()
    return {"items": [spec["build"](row) for row in rows], "count": count, "sync": sync}


# --------------------------------------------------------------------------
# 飞书消息落库
# --------------------------------------------------------------------------


def _insert_record(connection, message, extra_json):
    """在 records 表里新建一条协作记录，返回其 id。"""
    cursor = connection.execute(
        """INSERT INTO records (work_date, category, member, channel, summary, extra_json)
           VALUES (?, 'collaborations', ?, ?, ?, ?)""",
        (
            message["work_date"],
            message["member_name"],
            message["chat_name"],
            message["summary"],
            extra_json,
        ),
    )
    return cursor.lastrowid


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
    """把飞书消息幂等写入 feishu_messages / records，并记录本次同步运行。"""
    new_count = 0
    for message in messages:
        extra_json = json.dumps(
            {
                "source_type": "feishu",
                "message_id": message["message_id"],
                "chat_id": message["chat_id"],
                "sender_id": message["sender_id"],
                "msg_type": message["msg_type"],
                "created_at_utc": message["created_at_utc"],
                "collected_at_utc": message["collected_at_utc"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        existing = connection.execute(
            "SELECT id, record_id FROM feishu_messages WHERE message_id = ?",
            (message["message_id"],),
        ).fetchone()

        record_id = existing["record_id"] if existing else None
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
            record_id = _insert_record(connection, message, extra_json)

        if existing:
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


# --------------------------------------------------------------------------
# 发布时的流水（飞书消息 / GitHub 提交）
# --------------------------------------------------------------------------

_STREAM_SOURCES = {
    # key: (标签, 流水名称, 取值字段, 带日期的 SQL, 不带日期的 SQL)
    "feishu": (
        "飞书",
        "飞书群聊消息",
        "summary",
        """SELECT member_name, summary FROM feishu_messages
           WHERE chat_id = ? AND work_date = ?
           ORDER BY created_at_utc ASC, id ASC""",
        """SELECT member_name, summary FROM feishu_messages
           WHERE chat_id = ? ORDER BY created_at_utc ASC, id ASC""",
    ),
    "github": (
        "GitHub",
        "GitHub 提交",
        "message",
        """SELECT member_name, message FROM github_commits
           WHERE repository = ? AND work_date = ?
           ORDER BY committed_at_utc ASC, id ASC""",
        """SELECT member_name, message FROM github_commits
           WHERE repository = ? ORDER BY committed_at_utc ASC, id ASC""",
    ),
}


def stream_for_publish(connection, selected_source, chat_id, repository, work_date=None):
    """按当前数据源取流水，去重后返回 (流水名称, 明细列表)。"""
    identifier = chat_id if selected_source == "feishu" else repository
    if selected_source not in _STREAM_SOURCES or not identifier:
        return None, []

    tag, label, column, dated_sql, all_sql = _STREAM_SOURCES[selected_source]
    if work_date:
        rows = connection.execute(dated_sql, (identifier, work_date)).fetchall()
    else:
        rows = connection.execute(all_sql, (identifier,)).fetchall()

    seen = set()
    items = []
    for row in rows:
        key = (row["member_name"], row[column])
        if key in seen:
            continue
        seen.add(key)
        items.append({"tag": tag, "member": row["member_name"], "summary": row[column]})
    return label, items[-STREAM_LIMIT:]


# --------------------------------------------------------------------------
# 飞书自动同步后台线程
# --------------------------------------------------------------------------

_auto_sync_stop = None
_auto_sync_last_failure = {}


def _auto_sync_run_once(database_path):
    """对所有已配置飞书群聊的用户拉取一次消息。"""
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
        started = utc_now_iso()
        try:
            chat_name, messages = collect_feishu_recent_messages(
                user["feishu_chat_id"],
                app_id=user["feishu_app_id"],
                chat_name=user["feishu_chat_name"],
            )
            with database_connection(database_path) as connection:
                new_count = persist_feishu_messages(
                    connection,
                    messages,
                    user["feishu_chat_id"],
                    chat_name,
                    started,
                    utc_now_iso(),
                    user_id=user["id"],
                    actor_name=user["display_name"],
                )
            print(
                f"[飞书自动同步] 用户 {user['display_name']} 群「{chat_name}」："
                f"获取 {len(messages)} 条，新增 {new_count} 条。"
            )
            _auto_sync_last_failure.pop(user["id"], None)
        except FeishuSourceError as error:
            _record_sync_failure(database_path, user, started, error)


def _record_sync_failure(database_path, user, started, error):
    """同一条错误只记一次，避免每 5 秒刷一条失败日志。"""
    message = str(error)[:240]
    if _auto_sync_last_failure.get(user["id"]) == message:
        return
    _auto_sync_last_failure[user["id"]] = message
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
                    utc_now_iso(),
                    message,
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


def _auto_sync_worker(database_path, stop_event):
    print(f"[飞书自动同步] 后台线程已启动，每 {FEISHU_AUTO_SYNC_INTERVAL_SECONDS} 秒拉取一次。")
    while not stop_event.is_set():
        try:
            _auto_sync_run_once(database_path)
        except Exception as error:  # 单次异常不终止线程
            print(f"[飞书自动同步] 未预期错误：{error}", file=sys.stderr)
        if stop_event.wait(FEISHU_AUTO_SYNC_INTERVAL_SECONDS):
            break
    print("[飞书自动同步] 后台线程已停止。")


def start_feishu_auto_sync(database_path):
    """启动飞书自动同步线程（幂等）。"""
    global _auto_sync_stop
    if _auto_sync_stop is not None:
        return
    import threading

    stop_event = threading.Event()
    _auto_sync_stop = stop_event
    threading.Thread(
        target=_auto_sync_worker,
        args=(database_path, stop_event),
        name="feishu-auto-sync",
        daemon=True,
    ).start()


def stop_feishu_auto_sync():
    global _auto_sync_stop
    if _auto_sync_stop is not None:
        _auto_sync_stop.set()
        _auto_sync_stop = None


# --------------------------------------------------------------------------
# 邮件收件人与发送记录
# --------------------------------------------------------------------------


def normalize_recipients(*emails):
    """去空、去重、校验格式，返回有序的收件邮箱列表。"""
    recipients, seen = [], set()
    for email in emails:
        address = (email or "").strip()
        if not address:
            continue
        if not is_valid_email(address):
            raise EmailNotificationError(f"收件邮箱格式不正确：{address}")
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        recipients.append(address)
    return recipients


def recipient_slots(recipients):
    """把收件人列表补齐成固定三个槽位，便于写库和写 session。"""
    slots = list(recipients[:3])
    return slots + [""] * (3 - len(slots))


def record_email_delivery(
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
    """记录一次邮件发送结果，供工作台展示最近状态。"""
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
            utc_now_iso(),
        ),
    )


# --------------------------------------------------------------------------
# 附件
# --------------------------------------------------------------------------


def save_uploaded_attachments(files, upload_folder):
    """保存日报照片，返回相对 uploads/ 的 POSIX 路径列表。"""
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
        if not (file.content_type or "").lower().startswith("image/"):
            raise ValueError("上传文件不是图片，请重新选择照片。")
        file.stream.seek(0, os.SEEK_END)
        size = file.stream.tell()
        file.stream.seek(0)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError("单张照片不能超过 6MB。")
        today = datetime.date.today()
        target_dir = upload_root / str(today.year) / f"{today.month:02d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{secrets.token_hex(16)}{extension}"
        try:
            file.save(str(target))
        except OSError as error:
            raise ValueError("照片保存失败，请检查项目目录权限。") from error
        finally:
            file.stream.close()
        saved.append(target.relative_to(upload_root).as_posix())
    return saved
