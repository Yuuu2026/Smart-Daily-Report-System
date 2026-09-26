"""脱敏审计事件写入辅助函数。调用方须在当前业务事务中使用。"""

import datetime
import json

from report_core import CATEGORY_ORDER


_SUMMARY_FIELDS = {
    "record_created": {"category"},
    "records_imported": {"record_count", "categories"},
    "published_item_created": {"has_source_url"},
    "daily_report_succeeded": {"attempts", "notification_count"},
    "daily_report_failed": {"attempts", "reason"},
    "github_sync_succeeded": {"commit_count"},
    "github_sync_failed": {"error_code"},
}


def _safe_summary(action, summary):
    if not isinstance(summary, dict):
        return {}
    allowed = _SUMMARY_FIELDS.get(action, set())
    result = {key: summary[key] for key in allowed if key in summary}
    if action == "record_created" and result.get("category") not in CATEGORY_ORDER:
        result.pop("category", None)
    if action == "records_imported" and isinstance(result.get("categories"), dict):
        counts = {}
        for category in CATEGORY_ORDER:
            try:
                counts[category] = max(0, int(result["categories"].get(category, 0)))
            except (TypeError, ValueError):
                counts[category] = 0
        result["categories"] = {
            category: counts[category] for category in CATEGORY_ORDER
        }
    if action == "github_sync_succeeded":
        try:
            result["commit_count"] = max(0, int(result.get("commit_count", 0)))
        except (TypeError, ValueError):
            result["commit_count"] = 0
    if action == "github_sync_failed" and result.get("error_code") != "source_unavailable":
        result.pop("error_code", None)
    for key, value in tuple(result.items()):
        if key != "categories" and not isinstance(value, (str, int, bool, type(None))):
            result.pop(key, None)
    return result


def log_audit(
    connection,
    action,
    target_type,
    *,
    actor_id=None,
    actor_name="信息采集系统",
    target_id=None,
    work_date=None,
    summary=None,
):
    safe_summary = _safe_summary(action, summary)
    happened_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    cursor = connection.execute(
        """INSERT INTO audit_events
           (happened_at, actor_id, actor_name, action, target_type, target_id, work_date, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            happened_at,
            actor_id,
            str(actor_name)[:80],
            action,
            target_type,
            target_id,
            work_date,
            json.dumps(safe_summary, ensure_ascii=False, sort_keys=True),
        ),
    )
    return cursor.lastrowid
