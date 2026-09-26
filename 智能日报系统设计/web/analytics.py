"""基于工作记录和发布信息的日期区间统计。"""

from report_core import CATEGORY_ORDER
from web.app import DATABASE_PATH, _work_date, database_connection


def build_statistics(start_date, end_date, database_path=DATABASE_PATH):
    start_date = _work_date(start_date)
    end_date = _work_date(end_date)
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期。")

    categories = {category: 0 for category in CATEGORY_ORDER}
    members = {}
    with database_connection(database_path) as connection:
        category_rows = connection.execute(
            """SELECT category, COUNT(*) AS amount FROM records
               WHERE work_date BETWEEN ? AND ? GROUP BY category""",
            (start_date, end_date),
        ).fetchall()
        record_total = connection.execute(
            "SELECT COUNT(*) FROM records WHERE work_date BETWEEN ? AND ?",
            (start_date, end_date),
        ).fetchone()[0]
        date_rows = connection.execute(
            """SELECT work_date, COUNT(*) AS amount FROM records
               WHERE work_date BETWEEN ? AND ? GROUP BY work_date ORDER BY work_date""",
            (start_date, end_date),
        ).fetchall()
        member_rows = connection.execute(
            """SELECT member, category, COUNT(*) AS amount FROM records
               WHERE work_date BETWEEN ? AND ? GROUP BY member, category ORDER BY member, category""",
            (start_date, end_date),
        ).fetchall()
        publisher_rows = connection.execute(
            """SELECT u.display_name, COUNT(*) AS amount FROM published_items p
               JOIN publisher_users u ON u.id = p.publisher_id
               WHERE p.work_date BETWEEN ? AND ?
               GROUP BY u.display_name ORDER BY u.display_name""",
            (start_date, end_date),
        ).fetchall()
        published_total = connection.execute(
            "SELECT COUNT(*) FROM published_items WHERE work_date BETWEEN ? AND ?",
            (start_date, end_date),
        ).fetchone()[0]

    for row in category_rows:
        if row["category"] in categories:
            categories[row["category"]] = row["amount"]
    for row in member_rows:
        buckets = members.setdefault(row["member"], {category: 0 for category in CATEGORY_ORDER})
        if row["category"] in buckets:
            buckets[row["category"]] = row["amount"]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "record_total": record_total,
        "categories": categories,
        "by_date": [{"work_date": row["work_date"], "count": row["amount"]} for row in date_rows],
        "by_member": [
            {"member": member, **counts, "total": sum(counts.values())}
            for member, counts in sorted(members.items())
        ],
        "published_total": published_total,
        "by_publisher": [
            {"publisher": row["display_name"], "count": row["amount"]}
            for row in publisher_rows
        ],
    }
