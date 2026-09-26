"""团队智能日报生成器的本机 Web 服务。"""

import argparse
import datetime
import json
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

# 始终按本文件的位置定位项目根目录，避免依赖启动时的当前目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from web.audit import log_audit
from web.github_source import GitHubSourceError, normalize_repository
from report_core import (
    CATEGORY_ORDER,
    RECORD_FIELDS,
    group_by_member,
    render_report,
    validate,
)

DATABASE_PATH = Path(__file__).resolve().parent / "daily_report.db"

CREATE_RECORDS_TABLE = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date TEXT NOT NULL,
    category TEXT NOT NULL,
    member TEXT NOT NULL,
    repo TEXT,
    message TEXT,
    task TEXT,
    progress TEXT,
    channel TEXT,
    summary TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}'
)
"""

CREATE_PUBLISHER_TABLES = """
CREATE TABLE IF NOT EXISTS publisher_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
CREATE TABLE IF NOT EXISTS published_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher_id INTEGER NOT NULL REFERENCES publisher_users(id),
    work_date TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_url TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_published_items_work_date
    ON published_items(work_date, id);
"""

CREATE_GITHUB_TABLE = """
CREATE TABLE IF NOT EXISTS github_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    sha TEXT NOT NULL,
    github_login TEXT NOT NULL DEFAULT '',
    synced_by_username TEXT NOT NULL DEFAULT '',
    member_name TEXT NOT NULL,
    work_date TEXT NOT NULL,
    message TEXT NOT NULL,
    additions INTEGER NOT NULL DEFAULT 0,
    deletions INTEGER NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    commit_url TEXT NOT NULL DEFAULT '',
    committed_at_utc TEXT NOT NULL,
    collected_at_utc TEXT NOT NULL,
    UNIQUE(repository, sha)
);
CREATE INDEX IF NOT EXISTS idx_github_commits_work_date
    ON github_commits(work_date, member_name, committed_at_utc);
CREATE TABLE IF NOT EXISTS github_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    commit_count INTEGER NOT NULL DEFAULT 0,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL,
    error_message TEXT
);
"""

CREATE_DAILY_JOB_TABLES = """
CREATE TABLE IF NOT EXISTS scheduled_report_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    report_markdown TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS user_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES publisher_users(id),
    work_date TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT,
    UNIQUE (user_id, work_date)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_report_runs_status_date
    ON scheduled_report_runs(status, work_date);
CREATE INDEX IF NOT EXISTS idx_user_notifications_user_created
    ON user_notifications(user_id, created_at DESC);
"""

CREATE_V5_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    happened_at TEXT NOT NULL,
    actor_id INTEGER REFERENCES publisher_users(id) ON DELETE SET NULL,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER,
    work_date TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_events_happened
    ON audit_events(happened_at DESC, id DESC);
"""


class DatabaseInitializationError(Exception):
    """数据库无法初始化时使用的中文错误。"""


@contextmanager
def database_connection(database_path):
    """提供带提交、回滚和关闭处理的 SQLite 连接。"""
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(database_path=DATABASE_PATH):
    """创建 SQLite 文件和 records 表；对外不泄露底层异常堆栈。"""
    try:
        with database_connection(database_path) as connection:
            connection.execute(CREATE_RECORDS_TABLE)
            connection.executescript(CREATE_PUBLISHER_TABLES)
            connection.executescript(CREATE_GITHUB_TABLE)
            github_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(github_commits)")
            }
            if "synced_by_username" not in github_columns:
                connection.execute(
                    "ALTER TABLE github_commits "
                    "ADD COLUMN synced_by_username TEXT NOT NULL DEFAULT ''"
                )
            connection.executescript(CREATE_DAILY_JOB_TABLES)
            connection.executescript(CREATE_V5_AUDIT_TABLE)
    except sqlite3.Error:
        raise DatabaseInitializationError(
            "数据库初始化失败，请检查数据库文件和目录权限。"
        ) from None


def restore_record(database_row):
    """从数据库行恢复原始类别记录，包括 extra_json 中的未知字段。"""
    row = dict(database_row)
    category = row["category"]
    if category not in RECORD_FIELDS:
        raise ValueError(f"未知记录类别：{category}")

    record = {field: row[field] for field in RECORD_FIELDS[category]}
    extra_fields = json.loads(row.get("extra_json") or "{}")
    if not isinstance(extra_fields, dict):
        raise ValueError("数据库中的 extra_json 格式无效。")
    record.update(extra_fields)
    return {
        "work_date": row["work_date"],
        "category": category,
        "record": record,
    }


def _duplicate_key(entry):
    """生成包含日期、类别和整条记录的顺序无关比较键。"""
    return (
        entry["work_date"],
        entry["category"],
        json.dumps(entry["record"], ensure_ascii=False, sort_keys=True),
    )


def find_duplicate_records(records, existing_records=()):
    """找出新记录与数据库或同批较早记录重复的项目，不修改或删除记录。

    records 中每项格式为 {"work_date": 日期, "category": 类别, "record": 原始记录}。
    existing_records 可为相同格式，也可直接传入含 extra_json 的数据库行。
    返回项包含新记录的一起始序号和重复来源，供后续接口生成提示。
    """
    existing_keys = {}
    for existing in existing_records:
        existing_data = dict(existing)
        if "record" in existing_data:
            normalized = existing_data
        else:
            normalized = restore_record(existing_data)
        existing_keys.setdefault(_duplicate_key(normalized), existing_data)

    seen_batch = {}
    duplicates = []
    for index, entry in enumerate(records, start=1):
        key = _duplicate_key(entry)
        if key in existing_keys:
            duplicates.append({
                "index": index,
                "category": entry["category"],
                "member": entry["record"].get("member"),
                "duplicate_of": "数据库已有记录",
            })
        elif key in seen_batch:
            duplicates.append({
                "index": index,
                "category": entry["category"],
                "member": entry["record"].get("member"),
                "duplicate_of": f"本批第 {seen_batch[key]} 条",
            })
        else:
            seen_batch[key] = index
    return duplicates


def _insert_record(connection, entry):
    """Insert one validated normalized entry using the shared V2 schema."""
    category = entry["category"]
    record = entry["record"]
    values = {
        "repo": None,
        "message": None,
        "task": None,
        "progress": None,
        "channel": None,
        "summary": None,
    }
    values.update({
        field: record[field]
        for field in RECORD_FIELDS[category]
        if field != "member"
    })
    extra_fields = {
        field: value
        for field, value in record.items()
        if field not in RECORD_FIELDS[category]
    }
    cursor = connection.execute(
        """INSERT INTO records (
            work_date, category, member, repo, message, task, progress,
            channel, summary, extra_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["work_date"],
            category,
            record["member"],
            values["repo"],
            values["message"],
            values["task"],
            values["progress"],
            values["channel"],
            values["summary"],
            json.dumps(extra_fields, ensure_ascii=False, sort_keys=True),
        ),
    )
    return cursor.lastrowid


def build_report_text(work_date, database_path=DATABASE_PATH):
    """Load known fields for one date and render a Markdown report."""
    data = {category: [] for category in CATEGORY_ORDER}
    columns = "category, member, repo, message, task, progress, channel, summary"
    with database_connection(database_path) as connection:
        rows = connection.execute(
            f"SELECT {columns} FROM records WHERE work_date = ? ORDER BY id",
            (work_date,),
        ).fetchall()
        published_rows = connection.execute(
            """SELECT p.work_date, p.title, p.content, p.source_url,
                      u.display_name AS member
               FROM published_items p
               JOIN publisher_users u ON u.id = p.publisher_id
               WHERE p.work_date = ?
               ORDER BY p.id""",
            (work_date,),
        ).fetchall()
        github_rows = connection.execute(
            """SELECT member_name AS member, message, additions, deletions,
                      changed_files, commit_url, committed_at_utc
               FROM github_commits WHERE work_date = ?
               ORDER BY committed_at_utc, id""",
            (work_date,),
        ).fetchall()

    for row in rows:
        category = row["category"]
        if category not in RECORD_FIELDS:
            continue
        data[category].append({
            field: row[field]
            for field in RECORD_FIELDS[category]
        })

    groups = group_by_member(data)
    empty_text = (
        "今日无记录"
        if work_date == datetime.date.today().isoformat()
        else "所选日期无记录"
    )
    report = render_report(groups, work_date, empty_text=empty_text)
    report += "\n\n## 发布信息\n"
    if published_rows:
        by_member = {}
        for row in published_rows:
            by_member.setdefault(row["member"], []).append(row)
        for member in sorted(by_member):
            report += f"\n### {member}\n"
            for row in by_member[member]:
                report += f"- **{row['title']}**：{row['content']}"
                if row["source_url"]:
                    report += f"（[链接]({row['source_url']})）"
                report += "\n"
    else:
        report += f"{empty_text}\n"
    report += "\n## GitHub 提交\n"
    if github_rows:
        for row in github_rows:
            message = row["message"].splitlines()[0] if row["message"] else "（无提交说明）"
            report += (
                f"- **{row['member']}**：{message}"
                f"（+{row['additions']} / -{row['deletions']}，"
                f"{row['changed_files']} 个文件）"
            )
            if row["commit_url"]:
                report += f"（[提交]({row['commit_url']})）"
            report += "\n"
    else:
        report += f"{empty_text}\n"
    return report


def _work_date(value):
    """验证 YYYY-MM-DD 日期；未提供时使用本机当前日期。"""
    if value is None:
        return datetime.date.today().isoformat()
    if not isinstance(value, str):
        raise ValueError("日期必须是 YYYY-MM-DD 格式。")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期必须是有效的 YYYY-MM-DD 日期。") from None
    if parsed.isoformat() != value:
        raise ValueError("日期必须是 YYYY-MM-DD 格式。")
    return value


def create_app(database_path=DATABASE_PATH):
    """创建 Flask 应用。后续版本任务在此基础上增加路由。"""
    app = Flask(__name__)
    app.config.update(
        DATABASE_PATH=str(database_path),
        SESSION_COOKIE_NAME="daily_report_collector_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.secret_key = os.environ.get("DAILY_REPORT_SECRET_KEY") or secrets.token_hex(32)

    @app.errorhandler(sqlite3.Error)
    def handle_database_error(_error):
        return Response(
            "数据库操作失败，请检查数据库状态。",
            status=500,
            content_type="text/plain; charset=utf-8",
        )

    @app.get("/")
    def home():
        return redirect(url_for("archive_page"))

    @app.get("/archive")
    def archive_page():
        try:
            work_date = _work_date(request.args.get("date"))
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")
        repository = request.args.get("repository", "").strip()
        if repository:
            try:
                repository = normalize_repository(repository)
            except GitHubSourceError as error:
                return Response(str(error), status=400, content_type="text/plain; charset=utf-8")
        else:
            repository = ""
        with database_connection(app.config["DATABASE_PATH"]) as connection:
            items = connection.execute(
                """SELECT p.id, p.work_date, p.title, p.content, p.source_url, p.created_at,
                          u.display_name AS member
                   FROM published_items p
                   JOIN publisher_users u ON u.id = p.publisher_id
                   WHERE p.work_date = ?
                   ORDER BY p.created_at DESC, p.id DESC""",
                (work_date,),
            ).fetchall()
            if repository:
                github_items = connection.execute(
                    """SELECT id, work_date, message, additions, deletions, changed_files,
                              commit_url, committed_at_utc, member_name AS member,
                              synced_by_username
                       FROM github_commits WHERE work_date = ? AND repository = ?
                       ORDER BY committed_at_utc DESC, id DESC""",
                    (work_date, repository),
                ).fetchall()
            else:
                github_items = connection.execute(
                    """SELECT id, work_date, message, additions, deletions, changed_files,
                              commit_url, committed_at_utc, member_name AS member,
                              synced_by_username
                       FROM github_commits WHERE work_date = ?
                       ORDER BY committed_at_utc DESC, id DESC""",
                    (work_date,),
                ).fetchall()
        grouped = {}
        for item in items:
            item = dict(item)
            item["source_type"] = "成员发布"
            timestamp = datetime.datetime.fromisoformat(item["created_at"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
            item["local_created_at"] = timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
            grouped.setdefault(item["member"], []).append(item)
        for row in github_items:
            committed = datetime.datetime.fromisoformat(row["committed_at_utc"])
            message = row["message"].strip()
            title = message.splitlines()[0] if message else "无提交说明"
            item = {
                "id": row["id"], "created_at": row["committed_at_utc"],
                "local_created_at": committed.astimezone().strftime("%Y-%m-%d %H:%M"),
                "title": title, "content": f"{message}\n\n变更：+{row['additions']} / -{row['deletions']} 行 · {row['changed_files']} 个文件",
                "source_url": row["commit_url"], "member": row["member"],
                "synced_by_username": row["synced_by_username"],
                "source_type": "GitHub 提交",
            }
            grouped.setdefault(item["member"], []).append(item)
        for member_items in grouped.values():
            member_items.sort(key=lambda entry: entry["created_at"], reverse=True)
        return render_template(
            "archive.html",
            work_date=work_date,
            repository=repository,
            groups=grouped,
            item_count=len(items) + len(github_items),
        )

    @app.post("/records")
    def create_record():
        return jsonify(error="采集端仅展示成员发布内容，请前往成员发布页面提交信息。"), 410

    @app.post("/import")
    def import_records():
        return jsonify(error="采集端不再接受 JSON 导入，请通过成员发布页面提交信息。"), 410

    @app.get("/report")
    def report_page():
        return redirect(url_for("archive_page", date=request.args.get("date")))

    @app.get("/report.md")
    def download_report():
        return redirect(url_for("archive_page", date=request.args.get("date")))

    @app.get("/jobs")
    def jobs_page():
        return redirect(url_for("archive_page"))

    @app.get("/stats")
    def statistics_page():
        return redirect(url_for("archive_page"))

    @app.get("/audit")
    def audit_page():
        return redirect(url_for("archive_page"))

    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="启动本机团队智能日报服务")
    parser.add_argument("--port", type=int, default=5000, help="服务端口，默认 5000")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        initialize_database()
    except DatabaseInitializationError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    create_app().run(host="127.0.0.1", port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
