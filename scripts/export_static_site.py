"""把本机归档数据导出成纯静态站点，供 GitHub Pages 发布。

用法：
    python scripts/export_static_site.py                # 导出到 dist_site/
    python scripts/export_static_site.py --with-attachments
    python scripts/export_static_site.py --out D:\\site

设计说明：
- 只读打开 SQLite，不会改动生产数据。
- 复用 web/static/style.css，视觉与 5000 端归档页保持一致。
- 输出为扁平结构：index.html + 每个日期一个 HTML + assets/，
  这样 GitHub Pages 上不依赖任何服务端路由。
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "web" / "daily_report.db"
DEFAULT_OUT = PROJECT_ROOT / "dist_site"
TEMPLATE_DIR = Path(__file__).resolve().parent / "static_templates"
STATIC_DIR = PROJECT_ROOT / "web" / "static"
UPLOAD_DIR = PROJECT_ROOT / "uploads"

SITE_NAME = "团队智能日报"
SITE_SUBTITLE = "成员工作记录公开归档"
SIDEBAR_LIMIT = 40
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _weekday_label(work_date: str) -> str:
    try:
        return WEEKDAYS[datetime.date.fromisoformat(work_date).weekday()]
    except ValueError:
        return ""


def _local_time(value: str) -> str:
    """把库里存的时间转成本机时区展示，规则与 5000 端归档页一致。"""
    try:
        moment = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _images(raw, attachments_enabled: bool) -> list[str]:
    if not attachments_enabled:
        return []
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    return [str(name) for name in items if str(name).strip()]


def load_reports(db_path: Path, with_attachments: bool = False) -> dict[str, dict]:
    """按工作日期汇总 published_items 与 github_commits。"""
    if not db_path.exists():
        raise SystemExit(f"错误：找不到数据库 {db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        published = connection.execute(
            """SELECT p.id, p.work_date, p.title, p.content, p.created_at,
                      p.source_url, p.attachments, u.display_name AS member
               FROM published_items p
               JOIN publisher_users u ON u.id = p.publisher_id
               ORDER BY p.created_at DESC, p.id DESC"""
        ).fetchall()
        try:
            commits = connection.execute(
                """SELECT id, work_date, message, additions, deletions, changed_files,
                          commit_url, committed_at_utc, member_name AS member,
                          synced_by_username
                   FROM github_commits
                   ORDER BY committed_at_utc DESC, id DESC"""
            ).fetchall()
        except sqlite3.Error:
            commits = []
    finally:
        connection.close()

    reports: dict[str, dict] = {}

    def bucket(work_date: str) -> dict:
        return reports.setdefault(
            work_date, {"date": work_date, "weekday": _weekday_label(work_date), "items": []}
        )

    for row in published:
        bucket(row["work_date"])["items"].append({
            "kind": "published",
            "member": row["member"],
            "title": row["title"],
            "content": row["content"] or "",
            "source_type": "成员发布",
            "created_at": row["created_at"],
            "local_created_at": _local_time(row["created_at"]),
            "source_url": row["source_url"] or "",
            "synced_by": "",
            "images": _images(row["attachments"], with_attachments),
        })

    for row in commits:
        message = (row["message"] or "").strip()
        title = message.splitlines()[0] if message else "无提交说明"
        bucket(row["work_date"])["items"].append({
            "kind": "github",
            "member": row["member"],
            "title": title,
            "content": f"{message}\n\n变更：+{row['additions']} / -{row['deletions']} 行 · "
                       f"{row['changed_files']} 个文件",
            "source_type": "GitHub 提交",
            "created_at": row["committed_at_utc"],
            "local_created_at": _local_time(row["committed_at_utc"]),
            "source_url": row["commit_url"] or "",
            "synced_by": row["synced_by_username"] or "",
            "images": [],
        })

    for report in reports.values():
        report["items"].sort(key=lambda item: item["created_at"], reverse=True)
        grouped: dict[str, list] = {}
        for item in report["items"]:
            grouped.setdefault(item["member"], []).append(item)
        report["groups"] = [(member, grouped[member]) for member in sorted(grouped)]
        report["members"] = sorted(grouped)
        report["count"] = len(report["items"])
        report["titles"] = [item["title"] for item in report["items"]]
        report["file"] = f"{report['date']}.html"

    return reports


def copy_assets(out_dir: Path) -> None:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATIC_DIR / "style.css", assets / "style.css")


def copy_attachments(out_dir: Path, reports: dict[str, dict]) -> int:
    copied = 0
    for report in reports.values():
        for item in report["items"]:
            for name in item["images"]:
                source = (UPLOAD_DIR / name).resolve()
                try:
                    source.relative_to(UPLOAD_DIR.resolve())
                except ValueError:
                    continue
                if not source.is_file():
                    continue
                target = out_dir / "uploads" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied += 1
    return copied


def render(out_dir: Path, reports: dict[str, dict], with_attachments: bool) -> list[Path]:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ordered = [reports[key] for key in sorted(reports, reverse=True)]
    sidebar = [
        {"date": report["date"], "weekday": report["weekday"], "count": report["count"],
         "file": report["file"]}
        for report in ordered[:SIDEBAR_LIMIT]
    ]

    written: list[Path] = []
    index_html = env.get_template("index.html").render(
        site_name=SITE_NAME,
        site_subtitle=SITE_SUBTITLE,
        reports=ordered,
        sidebar=sidebar,
        total_items=sum(report["count"] for report in ordered),
        total_dates=len(ordered),
        total_members=len({member for report in ordered for member in report["members"]}),
        latest_date=ordered[0]["date"] if ordered else "",
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        with_attachments=with_attachments,
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    written.append(out_dir / "index.html")

    for position, report in enumerate(ordered):
        newer = ordered[position - 1] if position > 0 else None
        older = ordered[position + 1] if position + 1 < len(ordered) else None
        page = env.get_template("report.html").render(
            site_name=SITE_NAME,
            site_subtitle=SITE_SUBTITLE,
            report=report,
            sidebar=sidebar,
            newer={"date": newer["date"], "file": newer["file"]} if newer else None,
            older={"date": older["date"], "file": older["file"]} if older else None,
            total_items=sum(entry["count"] for entry in ordered),
            total_dates=len(ordered),
            generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            with_attachments=with_attachments,
        )
        target = out_dir / report["file"]
        target.write_text(page, encoding="utf-8")
        written.append(target)

    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出静态日报归档站点")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录，默认 dist_site")
    parser.add_argument("--with-attachments", action="store_true", help="同时导出成员上传的图片")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()

    reports = load_reports(db_path, with_attachments=args.with_attachments)
    if not reports:
        print("警告：数据库中没有可导出的日报记录。")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    copy_assets(out_dir)
    pages = render(out_dir, reports, args.with_attachments)
    image_count = copy_attachments(out_dir, reports) if args.with_attachments else 0

    print(f"已导出 {len(pages)} 个页面，覆盖 {len(reports)} 个日期，"
          f"{sum(report['count'] for report in reports.values())} 条记录"
          + (f"，{image_count} 张图片" if image_count else ""))
    print(f"输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
