"""第二套系统：登录后发布信息，数据写入日报系统共享 SQLite。"""

import argparse
import datetime
import functools
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

from report_quality import quality_warnings

from web.app import DATABASE_PATH, database_connection, initialize_database
from web.audit import log_audit
from web.email_notifications import (
    EmailNotificationError,
    is_valid_email,
    local_sender_email,
    send_progress_email,
    send_test_email,
    smtp_settings_from_env,
    store_smtp_password,
    write_local_smtp_config,
)
from web.feishu_source import (
    FeishuSourceError,
    collect_feishu_recent_messages,
    list_feishu_chats,
    store_feishu_app_secret,
)
from web.github_source import (
    GitHubSourceError,
    collect_last_24_hours,
    normalize_repository,
    validate_repository,
)
from web.publisher_auth import hash_password, login_required, verify_password
from web.publisher_repo import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    activity_snapshot,
    date_window,
    feishu_item,
    github_item,
    local_label,
    normalize_recipients,
    parse_page,
    persist_feishu_messages,
    published_item,
    recipient_slots,
    record_email_delivery,
    save_uploaded_attachments,
    start_feishu_auto_sync,
    stop_feishu_auto_sync,
    stream_for_publish,
)

# 列表页每页条数
ARCHIVE_PAGE_SIZE = 6
GITHUB_PAGE_SIZE = 8
FEISHU_PAGE_SIZE = 8
RECENT_CODE_CHANGES_PATH = PROJECT_ROOT / ".claude" / "recent_code_changes.json"

CSRF_ERROR = "页面已过期，请刷新后重试。"


def recent_code_changes(path=RECENT_CODE_CHANGES_PATH):
    """读取本机编辑摘要；缺失或损坏时不影响归档页。"""
    try:
        changes = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(changes, list):
        return []
    result = []
    for change in changes[:5]:
        if not isinstance(change, dict):
            continue
        if not all(isinstance(change.get(key), str) for key in ("path", "summary", "kind", "time")):
            continue
        if not all(type(change.get(key)) is int for key in ("additions", "deletions")):
            continue
        result.append({
            "path": change["path"][:160],
            "summary": change["summary"][:60],
            "kind": change["kind"][:20],
            "time": change["time"][:35],
            "additions": min(max(0, change["additions"]), 999999),
            "deletions": min(max(0, change["deletions"]), 999999),
        })
    return result


def format_code_changes(changes):
    """只用经过长度限制的元数据，生成可存入归档的文本。"""
    return "\n".join(
        f"{item['summary']} · {item['path']}（{item['kind']}，"
        f"相对 HEAD 累计 +{item['additions']} / −{item['deletions']}，"
        f"{item['time'][:16].replace('T', ' ')}）"
        for item in changes
    )


MAX_CODE_CHANGES_TEXT = 4000


def chosen_code_changes(all_changes):
    """按工作台表单的勾选决定本次发送哪段代码改动。

    表单带 code_changes_present 标记时以勾选和用户落笔的内容为准；
    没有该标记（旧表单或脚本调用）沿用「全部附加」的旧行为。
    """
    if request.form.get("code_changes_present") != "1":
        return all_changes, format_code_changes(all_changes)
    if not all_changes or request.form.get("include_code_changes") != "1":
        return [], ""
    typed = request.form.get("code_changes_text", "").strip()
    if not typed:
        return [], ""
    return all_changes, typed[:MAX_CODE_CHANGES_TEXT]


def csrf_token():
    """取得（必要时生成并写入会话）当前请求的 CSRF token。"""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_valid():
    """校验表单里带回的 token 与会话中的一致。"""
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


def csrf_protected(view):
    """表单 POST 统一做 CSRF 校验，失败返回 400 纯文本。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not csrf_valid():
            return Response(
                CSRF_ERROR, status=400, content_type="text/plain; charset=utf-8"
            )
        return view(*args, **kwargs)

    return wrapper


def csrf_redirect(endpoint="home", **values):
    """CSRF 校验失败的页面式处理：闪提示后跳回指定页面。"""
    flash(CSRF_ERROR, "error")
    return redirect(url_for(endpoint, **values))


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
                    if not row["read_at"]:
                        unread_notifications += 1
                    notifications.append({
                        "id": row["id"],
                        "title": row["title"],
                        "read": bool(row["read_at"]),
                        "created_at": local_label(row["created_at"]),
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

    def save_email_channel(
        sender_email,
        recipient_email,
        smtp_password,
        recipient_email_2="",
        recipient_email_3="",
    ):
        sender_email = (sender_email or "").strip()
        recipients = normalize_recipients(
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

        slot_1, slot_2, slot_3 = recipient_slots(recipients)
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            connection.execute(
                """UPDATE publisher_users
                   SET sender_email = ?, notification_email = ?,
                       notification_email_2 = ?, notification_email_3 = ?
                   WHERE id = ?""",
                (sender_email, slot_1, slot_2, slot_3, session["user_id"]),
            )
        session.update(
            sender_email=sender_email,
            notification_email=slot_1,
            notification_email_2=slot_2,
            notification_email_3=slot_3,
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
        recent_changes=None,
        recent_changes_text=None,
    ):
        """给多个收件人分别发送邮件，并逐条记录发送结果。"""
        settings = smtp_settings_from_env(sender_email)
        if recent_changes is None and recent_changes_text is None:
            changes = recent_code_changes()
        else:
            changes = recent_changes or []
        subject = f"研一 {session['display_name']} {work_date} 智能日报"
        sent, failed = [], []
        for recipient in recipients:
            error_message = ""
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
                    recent_changes=changes,
                    recent_changes_text=recent_changes_text,
                )
            except EmailNotificationError as error:
                error_message = str(error)
                failed.append((recipient, error_message))
            else:
                sent.append(recipient)
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                record_email_delivery(
                    connection,
                    session["user_id"],
                    sender_email,
                    recipient,
                    subject,
                    delivery_type,
                    work_date,
                    "failed" if error_message else "sent",
                    error_message,
                )
        return sent, failed

    @app.get("/uploads/<path:filename>")
    @login_required
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.get("/sources")
    @login_required
    def select_source():

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
            page_title="接入配置",
            breadcrumb_title="接入配置",
            breadcrumb_subtitle="选择 GitHub 或飞书",
            search_target=url_for("archive_page"),
        )

    @app.get("/")
    @login_required
    def entry():
        """账号进入系统后的统一落地页：先看团队日报归档。"""
        return redirect(url_for("archive_page"))

    @app.get("/workbench")
    @login_required
    def home():
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
        # GitHub 已选定但未选择仓库时，不再强制跳转到接入设置；
        # 直接渲染工作台，并在工作台内显示「仓库接入」引导卡片。
        github_needs_setup = bool(
            selected_source == "github" and not selected_repository
        )
        # 飞书已选定但未配置群聊时，不再强制跳转到接入设置；
        # 直接渲染工作台，并在工作台内显示「群聊接入」引导卡片。
        feishu_needs_setup = bool(
            selected_source == "feishu" and not selected_feishu_chat_id
        )
        # 已选定群聊后，仍支持从工作台重新唤出「群聊选择」面板（切换群聊）。
        feishu_reselect = bool(
            selected_source == "feishu" and request.args.get("reselect_chat") == "1"
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
            activity_scope = (
                selected_repository
                if selected_source == "github"
                else selected_feishu_chat_id
            )
            snapshot = activity_snapshot(connection, selected_source, activity_scope)
            activity_items = snapshot["items"]
            activity_count = snapshot["count"]
            sync_status = snapshot["sync"]
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
                "created_at": local_label(delivery["created_at"]),
            })
        workbench_changes = recent_code_changes()
        workspace_label = "当前仓库" if selected_source == "github" else "当前群聊"
        if selected_source == "github" and selected_repository:
            workspace_name = selected_repository
            workspace_caption = "已连接 GitHub 仓库"
        elif selected_source == "github":
            workspace_name = "未选择仓库"
            workspace_caption = "请先在下方接入 GitHub 仓库"
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
            recent_changes=workbench_changes,
            code_changes_text=format_code_changes(workbench_changes),
            code_changes_editable=True,
            latest_email_delivery=email_deliveries[0] if email_deliveries else None,
            publish_only=publish_only,
            github_needs_setup=github_needs_setup,
            current_repository=session.get("selected_repository") or "",
            feishu_needs_setup=feishu_needs_setup,
            feishu_reselect=feishu_reselect,
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
    @login_required
    def archive_page():
        try:
            date_from, date_to = date_window(
                request.args.get("from"),
                request.args.get("to"),
                default_days=30,
            )
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")

        query = request.args.get("q", "").strip().lower()
        page = parse_page(request.args.get("page"))
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

        published_items = [published_item(row) for row in published_rows]
        github_items = [github_item(row) for row in github_rows]
        feishu_items = [feishu_item(row) for row in feishu_rows]
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
            recent_changes=recent_code_changes(),
        )

    @app.get("/github")
    @login_required
    def github_activity_page():
        try:
            date_from, date_to = date_window(
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
        page = parse_page(request.args.get("page"))
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

        items = [github_item(row) for row in rows]
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
            recent_changes=recent_code_changes(),
        )

    @app.get("/feishu")
    @login_required
    def feishu_page():
        try:
            date_from, date_to = date_window(
                request.args.get("from"),
                request.args.get("to"),
                default_days=30,
            )
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")

        query = request.args.get("q", "").strip().lower()
        page = parse_page(request.args.get("page"))
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

        items = [feishu_item(row) for row in rows]
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
            recent_changes=recent_code_changes(),
        )

    @app.post("/feishu/settings")
    @login_required
    def save_feishu_settings():
        if not csrf_valid():
            return csrf_redirect("home", _anchor="activity-stream")

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
        # 若来自工作台的「切换群聊」，验证后保持面板展开，便于直接选择目标群聊。
        if request.form.get("reselect") == "1":
            return redirect(url_for("home", reselect_chat=1))
        return redirect(url_for("home"))

    @app.post("/feishu/sync")
    @login_required
    def sync_feishu():
        if not csrf_valid():
            return csrf_redirect("feishu_page")

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
    @login_required
    def send_feishu_messages():
        if not csrf_valid():
            return csrf_redirect("feishu_page")

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
        recipients = normalize_recipients(
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
            stream_label, stream_items = stream_for_publish(
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
    @login_required
    def select_repository():
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
            return redirect(url_for("home"))
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
            return redirect(url_for("archive_page"))
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if "user_id" in session:
            return redirect(url_for("archive_page"))
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
            return redirect(url_for("archive_page"))
        return render_template("register.html")

    @app.post("/logout")
    @csrf_protected
    def logout():
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
    @login_required
    def save_email_settings():
        if not csrf_valid():
            return csrf_redirect("home", _anchor="daily-form")
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
    @login_required
    def test_email_settings():
        if not csrf_valid():
            return csrf_redirect("home", _anchor="daily-form")
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
    @login_required
    def save_progress_draft():
        if not csrf_valid():
            return csrf_redirect("home", _anchor="daily-form")
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
    @login_required
    def publish():
        if not csrf_valid():
            return csrf_redirect("home")
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
        changes, changes_text = chosen_code_changes(recent_code_changes())
        if not any((finished, problems, next_steps, changes_text, title and content)):
            flash("请填写下一步计划，或勾选要随日报发送的代码改动后再发布。", "error")
            return redirect(url_for("home", _anchor="daily-form"))
        if any(value for _, value in progress_sections) or changes_text:
            title = title or "今日工作进展"
            sections = [
                f"{label}：\n{value}" for label, value in progress_sections if value
            ]
            if changes_text:
                sections.append(f"最近代码改动：\n{changes_text}")
            content = f"工作状态：{work_status}\n\n" + "\n\n".join(sections)
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
            attachment_paths = save_uploaded_attachments(
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
                stream_label, stream_items = stream_for_publish(
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
                recent_changes=changes,
                recent_changes_text=changes_text,
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
    @login_required
    def sync_github():
        if not csrf_valid():
            return csrf_redirect("home")
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
    @login_required
    def send_github_commits():
        if not csrf_valid():
            return csrf_redirect("github_activity_page")

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
        recipients = normalize_recipients(
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
            stream_label, stream_items = stream_for_publish(
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
    @login_required
    @csrf_protected
    def mark_notification_read(notification_id):
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
    @login_required
    @csrf_protected
    def mark_all_notifications_read():
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
