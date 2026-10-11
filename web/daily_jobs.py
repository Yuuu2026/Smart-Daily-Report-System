"""定时日报执行、幂等处理和站内通知。"""

import datetime
from web.app import DATABASE_PATH, build_report_text, database_connection, initialize_database, _work_date
from web.audit import log_audit


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _claim_run(work_date, database_path, now):
    """以事务领取某日任务；成功项跳过，失败或超时项可重试。"""
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id, status, attempts, started_at FROM scheduled_report_runs WHERE work_date = ?",
            (work_date,),
        ).fetchone()
        if existing and existing["status"] == "succeeded":
            return {"claimed": False, "status": "succeeded", "attempts": existing["attempts"]}
        if existing and existing["status"] == "running":
            try:
                started = datetime.datetime.fromisoformat(existing["started_at"])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=datetime.timezone.utc)
                age = now - started.astimezone(datetime.timezone.utc)
            except ValueError:
                age = datetime.timedelta(minutes=16)
            if age < datetime.timedelta(minutes=15):
                return {"claimed": False, "status": "running", "attempts": existing["attempts"]}
            connection.execute(
                "UPDATE scheduled_report_runs SET status = 'failed', error_message = ? WHERE id = ?",
                ("检测到超过 15 分钟未结束的任务，允许重新执行。", existing["id"]),
            )

        if existing:
            attempts = existing["attempts"] + 1
            connection.execute(
                """UPDATE scheduled_report_runs
                   SET status = 'running', attempts = ?, started_at = ?, completed_at = NULL,
                       report_markdown = NULL, error_message = NULL
                   WHERE id = ?""",
                (attempts, now.isoformat(timespec="seconds"), existing["id"]),
            )
            run_id = existing["id"]
        else:
            attempts = 1
            cursor = connection.execute(
                """INSERT INTO scheduled_report_runs(work_date,status,attempts,started_at)
                   VALUES (?, 'running', ?, ?)""",
                (work_date, attempts, now.isoformat(timespec="seconds")),
            )
            run_id = cursor.lastrowid
        return {"claimed": True, "id": run_id, "status": "running", "attempts": attempts}


def run_daily_report(work_date, database_path=DATABASE_PATH):
    """为指定日期生成一次日报并通知启用账号；失败任务可安全重试。"""
    normalized_date = None
    try:
        work_date = _work_date(work_date)
        normalized_date = work_date
        initialize_database(database_path)
        now = datetime.datetime.now(datetime.timezone.utc)
        claim = _claim_run(work_date, database_path, now)
        if not claim["claimed"]:
            return {
                "status": claim["status"],
                "skipped": True,
                "attempts": claim["attempts"],
                "message": "该日期的日报任务已经成功。" if claim["status"] == "succeeded" else "该日期的日报任务正在运行。",
            }

        report = build_report_text(work_date, database_path)
        completed_at = _utc_now()
        with database_connection(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE scheduled_report_runs
                   SET status = 'succeeded', completed_at = ?, report_markdown = ?, error_message = NULL
                   WHERE id = ? AND status = 'running'""",
                (completed_at, report, claim["id"]),
            )
            users = connection.execute(
                "SELECT id FROM publisher_users WHERE enabled = 1 ORDER BY id"
            ).fetchall()
            for user in users:
                connection.execute(
                    """INSERT OR IGNORE INTO user_notifications(user_id, work_date, title, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (user["id"], work_date, f"{work_date} 团队日报已生成", completed_at),
                )
            log_audit(
                connection,
                "daily_report_succeeded",
                "report_run",
                actor_name="自动日报任务",
                target_id=claim["id"],
                work_date=work_date,
                summary={"attempts": claim["attempts"], "notification_count": len(users)},
            )
        return {
            "status": "succeeded",
            "skipped": False,
            "attempts": claim["attempts"],
            "notification_count": len(users),
            "message": f"{work_date} 日报已生成，已通知 {len(users)} 个启用账号。",
        }
    except Exception:
        # 不将数据库异常、路径或敏感连接信息写入可查看的错误字段。
        try:
            initialize_database(database_path)
            with database_connection(database_path) as connection:
                cursor = connection.execute(
                    """UPDATE scheduled_report_runs
                       SET status = 'failed', completed_at = ?, error_message = ?
                       WHERE work_date = ? AND status = 'running'""",
                    (_utc_now(), "日报生成失败，请检查数据库权限和记录数据后重试。", normalized_date),
                )
                if cursor.rowcount:
                    run_row = connection.execute(
                        "SELECT id, attempts FROM scheduled_report_runs WHERE work_date = ?",
                        (normalized_date,),
                    ).fetchone()
                    log_audit(
                        connection,
                        "daily_report_failed",
                        "report_run",
                        actor_name="自动日报任务",
                        target_id=run_row["id"],
                        work_date=normalized_date,
                        summary={"attempts": run_row["attempts"], "reason": "generation_failed"},
                    )
        except Exception:
            pass
        return {
            "status": "failed",
            "skipped": False,
            "attempts": None,
            "message": "日报生成失败，请检查数据库权限和记录数据后重试。",
        }


def _schedule_time(value):
    try:
        parsed = datetime.datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        raise ValueError("调度时间须为 HH:MM，例如 18:00。") from None
    if parsed.strftime("%H:%M") != value:
        raise ValueError("调度时间须为 HH:MM，例如 18:00。")
    return parsed


def run_scheduler(database_path=DATABASE_PATH, schedule_time="18:00", stop_event=None, poll_seconds=15):
    """供主服务使用的本机时间调度循环，可传入 stop_event 便于受控关闭。"""
    target_time = _schedule_time(schedule_time)
    attempted_date = None
    while stop_event is None or not stop_event.is_set():
        local_now = datetime.datetime.now().astimezone()
        today = local_now.date().isoformat()
        if local_now.time().replace(tzinfo=None) >= target_time and attempted_date != today:
            run_daily_report(today, database_path)
            attempted_date = today
        if stop_event is None:
            import time
            time.sleep(poll_seconds)
        elif stop_event.wait(poll_seconds):
            break
