"""第二套系统：登录后发布信息，数据写入日报系统共享 SQLite。"""

import argparse
import datetime
import hmac
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.app import DATABASE_PATH, database_connection, initialize_database
from web.audit import log_audit
from web.github_source import (
    GitHubSourceError,
    collect_last_24_hours,
    validate_repository,
)
from web.publisher_auth import hash_password, verify_password


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
    def inject_csrf_token():
        return {"csrf_token": csrf_token}

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
        return render_template(
            "publish.html",
            display_name=session["display_name"],
            today=datetime.date.today().isoformat(),
            github_repository=selected_repository,
            archive_url=(
                "http://127.0.0.1:5000/archive?"
                + urlencode({"repository": selected_repository})
            ),
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
            archive_url = (
                "http://127.0.0.1:5000/archive?"
                + urlencode({"repository": selected_repository})
            )
            return redirect(archive_url)
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
        return redirect(url_for("home"))

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
        return redirect(url_for("home"))

    @app.get("/notifications")
    def notifications_page():
        return redirect(url_for("home"))

    @app.post("/notifications/<int:notification_id>/read")
    def mark_notification_read(notification_id):
        return redirect(url_for("notifications_page"))

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
