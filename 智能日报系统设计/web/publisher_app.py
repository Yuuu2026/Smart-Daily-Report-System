"""第二套系统：登录后发布信息，数据写入日报系统共享 SQLite。"""

import argparse
import datetime
import hmac
import math
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.app import DATABASE_PATH, database_connection, initialize_database
from web.audit import log_audit
from web.github_source import (
    GitHubSourceError,
    collect_last_24_hours,
    normalize_repository,
    validate_repository,
)
from web.publisher_auth import hash_password, verify_password


ARCHIVE_PAGE_SIZE = 6
GITHUB_PAGE_SIZE = 8


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


def _published_item(row):
    created_at = datetime.datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    local_created_at = created_at.astimezone()
    title = row["title"].strip() or "今日工作进展"
    content = (row["content"] or "").strip()
    return {
        "id": f"publish-{row['id']}",
        "work_date": row["work_date"],
        "created_at": row["created_at"],
        "local_created_at": local_created_at.strftime("%Y-%m-%d %H:%M"),
        "relative_time": _relative_time(row["created_at"]),
        "title": title,
        "content": content,
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
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("DAILY_REPORT_COOKIE_SECURE") == "1",
    )

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
                "display_name": session.get("display_name", ""),
                "username": session.get("username", ""),
            },
        }

    def csrf_valid():
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))

    @app.get("/")
    def home():
        if "user_id" not in session:
            return redirect(url_for("login"))
        selected_repository = session.get("selected_repository")
        if not selected_repository:
            return redirect(url_for("select_repository"))
        today = datetime.date.today()
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            commit_count = connection.execute(
                "SELECT COUNT(*) FROM github_commits WHERE repository = ? AND work_date = ?",
                (selected_repository, today.isoformat()),
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
        return render_template(
            "dashboard.html",
            display_name=session["display_name"],
            today=today.isoformat(),
            today_label=f"{today.year}年{today.month}月{today.day}日 · 星期{('一', '二', '三', '四', '五', '六', '日')[today.weekday()]}",
            github_repository=selected_repository,
            commit_count=commit_count,
            progress_count=task_count + published_count,
            collaboration_count=collaboration_count,
            active_page="workbench",
            page_title="工作台",
            breadcrumb_title="工作台",
            breadcrumb_subtitle="今日工作概览",
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

        with database_connection(app.config["DATABASE_PATH"]) as connection:
            if date_from and date_to:
                published_rows = connection.execute(
                    """SELECT p.id, p.work_date, p.title, p.content, p.source_url,
                              p.created_at, u.display_name AS member
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
            else:
                published_rows = connection.execute(
                    """SELECT p.id, p.work_date, p.title, p.content, p.source_url,
                              p.created_at, u.display_name AS member
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

        published_items = [_published_item(row) for row in published_rows]
        github_items = [_github_item(row) for row in github_rows]
        items = published_items + github_items
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
            active_page="github",
            page_title="GitHub 活动",
            breadcrumb_title="GitHub 活动",
            breadcrumb_subtitle="仓库活动流水",
            search_target=url_for("github_activity_page"),
        )

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
                    "SELECT id, username, password_hash, display_name FROM publisher_users WHERE username = ? AND enabled = 1",
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
            csrf_token()
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                log_audit(
                    connection,
                    "login_succeeded",
                    "session",
                    actor_id=user["id"],
                    actor_name=user["display_name"],
                )
            return redirect(url_for("home"))
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
            csrf_token()
            flash("账号创建成功，已自动登录。", "success")
            return redirect(url_for("home"))
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

    @app.post("/publish")
    def publish():
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not session.get("selected_repository"):
            return redirect(url_for("select_repository"))
        if not csrf_valid():
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("home"))
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        progress_sections = (
            ("今天完成了什么", request.form.get("finished", "").strip()),
            ("遇到了什么问题", request.form.get("problems", "").strip()),
            ("下一步准备做什么", request.form.get("next_steps", "").strip()),
        )
        if any(value for _, value in progress_sections):
            title = title or "今日工作进展"
            content = "\n\n".join(
                f"{label}：\n{value}"
                for label, value in progress_sections
                if value
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
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            cursor = connection.execute(
                """INSERT INTO published_items
                   (publisher_id, work_date, title, content, source_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session["user_id"], work_date, title, content, source_url or None, created_at),
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
            with database_connection(app.config["DATABASE_PATH"]) as connection:
                connection.execute(
                    """INSERT INTO github_sync_runs
                       (repository, status, commit_count, started_at_utc, completed_at_utc, error_message)
                       VALUES (?, 'failed', 0, ?, ?, ?)""",
                    (repository, started, completed, str(error)[:240]),
                )
                log_audit(connection, "github_sync_failed", "github_repository",
                          actor_id=actor_id, actor_name=actor_name,
                          summary={"error_code": "source_unavailable"})
            flash(f"GitHub 提取失败：{error}", "error")
        return redirect(url_for("github_activity_page", repository=repository))

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
    create_publisher_app().run(host="127.0.0.1", port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
