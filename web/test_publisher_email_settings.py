import tempfile
import unittest
import datetime
import io
import json
from pathlib import Path
from unittest.mock import patch

from web import email_notifications
from web.app import database_connection, initialize_database
from web.publisher_app import create_publisher_app, format_code_changes
from web.publisher_auth import hash_password
from web.publisher_repo import workload_series


class PublisherEmailSettingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "daily_report.db"
        self.config_path = Path(self.temp_dir.name) / ".smtp.local.json"
        self.original_config_path = email_notifications.LOCAL_SMTP_CONFIG_PATH
        email_notifications.LOCAL_SMTP_CONFIG_PATH = self.config_path
        initialize_database(self.database_path)
        with database_connection(self.database_path) as connection:
            connection.execute(
                """INSERT INTO publisher_users
                   (username, password_hash, display_name, sender_email, notification_email)
                   VALUES (?, ?, ?, ?, ?)""",
                ("zhangsan", hash_password("safe-password-123"), "张三", "", ""),
            )
        self.app = create_publisher_app(self.database_path, secret_key="test-secret")
        self.client = self.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session.update(
                {
                    "user_id": 1,
                    "username": "zhangsan",
                    "display_name": "张三",
                    "sender_email": "",
                    "notification_email": "",
                    "csrf_token": "test-csrf",
                }
            )

    def tearDown(self):
        email_notifications.LOCAL_SMTP_CONFIG_PATH = self.original_config_path
        self.temp_dir.cleanup()

    def post_email_settings(self, endpoint):
        return self.client.post(
            endpoint,
            data={
                "csrf_token": "test-csrf",
                "sender_email": "3247026746@qq.com",
                "recipient_email": "3247026746@qq.com",
            },
            follow_redirects=False,
        )

    def test_settings_are_saved_for_current_user(self):
        with patch.object(email_notifications, "_smtp_password_from_keyring", return_value=""):
            response = self.post_email_settings("/email/settings")

        self.assertEqual(response.status_code, 302)
        with database_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT sender_email, notification_email FROM publisher_users WHERE id = 1"
            ).fetchone()
        self.assertEqual(row["sender_email"], "3247026746@qq.com")
        self.assertEqual(row["notification_email"], "3247026746@qq.com")

    def test_test_endpoint_sends_with_configured_channel(self):
        with patch.object(email_notifications, "_smtp_password_from_keyring", return_value="auth-code"), \
             patch("web.publisher_app.send_test_email") as send_test_email:
            response = self.post_email_settings("/email/test")

        self.assertEqual(response.status_code, 302)
        send_test_email.assert_called_once()
        settings, recipient = send_test_email.call_args.args
        self.assertEqual(settings.host, "smtp.qq.com")
        self.assertEqual(settings.username, "3247026746@qq.com")
        self.assertEqual(recipient, "3247026746@qq.com")

    def test_publisher_pages_render_decoupled_and_structured_content(self):
        today = datetime.date.today().isoformat()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with database_connection(self.database_path) as connection:
            connection.execute(
                """INSERT INTO github_commits (
                       repository, sha, github_login, synced_by_username, member_name,
                       work_date, message, additions, deletions, changed_files,
                       commit_url, committed_at_utc, collected_at_utc
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "owner/repo",
                    "1234567890abcdef",
                    "zhangsan",
                    "zhangsan",
                    "张三",
                    today,
                    "重构日报结构化渲染",
                    12,
                    3,
                    2,
                    "https://github.com/owner/repo/commit/1234567890abcdef",
                    now,
                    now,
                ),
            )
        with self.client.session_transaction() as flask_session:
            flask_session["selected_repository"] = "owner/repo"

        workbench = self.client.get("/workbench")
        archive = self.client.get("/archive")
        github = self.client.get("/github")

        self.assertEqual(workbench.status_code, 200)
        self.assertNotIn("日报颗粒度", workbench.get_data(as_text=True))
        self.assertNotIn("今天完成了什么？", workbench.get_data(as_text=True))
        self.assertNotIn("遇到了什么问题？", workbench.get_data(as_text=True))
        self.assertIn("下一步准备做什么？", workbench.get_data(as_text=True))
        self.assertIn("最近代码改动", workbench.get_data(as_text=True))
        self.assertIn("添加照片", workbench.get_data(as_text=True))
        self.assertIn("目的邮箱 2", workbench.get_data(as_text=True))
        self.assertEqual(archive.status_code, 200)
        self.assertIn("日报归档", archive.get_data(as_text=True))
        self.assertEqual(github.status_code, 200)
        self.assertIn("重构日报结构化渲染", github.get_data(as_text=True))

        response = self.client.post(
            "/publish",
            data={
                "csrf_token": "test-csrf",
                "work_date": today,
                "status": "已完成",
                "source_url": "https://github.com/owner/repo/pull/128",
                "finished": "完成日报归档页与 GitHub 页解耦",
                "problems": "无阻塞问题",
                "next_steps": "补充页面级回归测试",
                "sender_email": "",
                "recipient_email": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        archive = self.client.get("/archive")
        archive_text = archive.get_data(as_text=True)
        self.assertIn("完成事项", archive_text)
        self.assertIn("完成日报归档页与 GitHub 页解耦", archive_text)
        self.assertIn("已完成", archive_text)

    def test_publish_save_send_sends_email_and_archives(self):
        today = datetime.date.today().isoformat()
        with patch(
            "web.publisher_app.smtp_settings_from_env", return_value="configured-smtp"
        ), patch("web.publisher_app.send_progress_email") as send_progress_email:
            response = self.client.post(
                "/publish",
                data={
                    "csrf_token": "test-csrf",
                    "work_date": today,
                    "status": "已完成",
                    "finished": "完成工作台与活动流水合并",
                    "problems": "无阻塞",
                    "next_steps": "验证一键发送归档",
                    "sender_email": "3247026746@qq.com",
                    "recipient_email": "recipient@qq.com",
                    "publish_action": "save_send",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        send_progress_email.assert_called_once()
        with database_connection(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM published_items WHERE work_date = ?",
                (today,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_publish_save_only_archives_without_sending_email(self):
        today = datetime.date.today().isoformat()
        with patch(
            "web.publisher_app.smtp_settings_from_env", return_value="configured-smtp"
        ), patch("web.publisher_app.send_progress_email") as send_progress_email:
            response = self.client.post(
                "/publish",
                data={
                    "csrf_token": "test-csrf",
                    "work_date": today,
                    "status": "进行中",
                    "finished": "只保存到归档",
                    "problems": "",
                    "next_steps": "",
                    "sender_email": "3247026746@qq.com",
                    "recipient_email": "recipient@qq.com",
                    "publish_action": "save_only",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        send_progress_email.assert_not_called()
        with database_connection(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM published_items WHERE work_date = ?",
                (today,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_publish_sends_to_multiple_recipients(self):
        today = datetime.date.today().isoformat()
        with patch(
            "web.publisher_app.smtp_settings_from_env", return_value="configured-smtp"
        ), patch("web.publisher_app.send_progress_email") as send_progress_email:
            response = self.client.post(
                "/publish",
                data={
                    "csrf_token": "test-csrf",
                    "work_date": today,
                    "finished": "验证多人发送",
                    "problems": "",
                    "next_steps": "",
                    "sender_email": "3247026746@qq.com",
                    "recipient_email": "one@example.com",
                    "recipient_email_2": "two@example.com",
                    "recipient_email_3": "three@example.com",
                    "publish_action": "save_send",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(send_progress_email.call_count, 3)
        with database_connection(self.database_path) as connection:
            rows = connection.execute(
                """SELECT recipient_email
                   FROM email_delivery_logs
                   WHERE user_id = 1 AND delivery_type = 'daily_report'
                   ORDER BY id"""
            ).fetchall()
            user = connection.execute(
                """SELECT notification_email,
                          notification_email_2,
                          notification_email_3
                   FROM publisher_users WHERE id = 1"""
            ).fetchone()
        self.assertEqual(
            [row["recipient_email"] for row in rows],
            ["one@example.com", "two@example.com", "three@example.com"],
        )
        self.assertEqual(user["notification_email"], "one@example.com")
        self.assertEqual(user["notification_email_2"], "two@example.com")
        self.assertEqual(user["notification_email_3"], "three@example.com")

    def test_publish_saves_photo_attachments(self):
        today = datetime.date.today().isoformat()
        upload_folder = Path(self.temp_dir.name) / "uploads"
        self.app.config["UPLOAD_FOLDER"] = str(upload_folder)
        with patch(
            "web.publisher_app.smtp_settings_from_env", return_value="configured-smtp"
        ), patch("web.publisher_app.send_progress_email") as send_progress_email:
            response = self.client.post(
                "/publish",
                data={
                    "csrf_token": "test-csrf",
                    "work_date": today,
                    "finished": "带照片的日报",
                    "problems": "",
                    "next_steps": "",
                    "sender_email": "",
                    "recipient_email": "",
                    "publish_action": "save_only",
                    "attachments": (
                        io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg"),
                        "progress.jpg",
                    ),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        with database_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT attachments FROM published_items WHERE work_date = ?",
                (today,),
            ).fetchone()
        attachments = json.loads(row["attachments"])
        self.assertEqual(len(attachments), 1)
        self.assertTrue((upload_folder / attachments[0]).exists())
        served = self.client.get(f"/uploads/{attachments[0]}")
        self.assertEqual(served.status_code, 200)
        served.close()

    def test_workbench_change_summary_renders_and_escapes_code_path(self):
        changes = [{"summary": "更新 Python 逻辑", "path": "web/<script>bad</script>.py",
                    "kind": "修改", "additions": 2, "deletions": 1,
                    "time": "2026-10-22T09:00+08:00"}]
        with self.client.session_transaction() as flask_session:
            flask_session["selected_source"] = "github"
        with patch("web.publisher_app.recent_code_changes", return_value=changes):
            page = self.client.get("/workbench").get_data(as_text=True)
        self.assertIn("更新 Python 逻辑", page)
        self.assertIn("web/&lt;script&gt;bad&lt;/script&gt;.py", page)
        self.assertNotIn("<script>bad</script>", page)
        self.assertNotIn('name="finished"', page)
        self.assertNotIn('name="problems"', page)
        self.assertNotIn('name="report_detail"', page)

    def test_next_steps_and_code_summary_are_saved_to_archive(self):
        today = datetime.date.today().isoformat()
        changes = [{"summary": "调整日报归档页面", "path": "web/archive.html",
                    "kind": "修改", "additions": 3, "deletions": 1,
                    "time": "2026-10-22T09:00+08:00"}]
        with patch("web.publisher_app.recent_code_changes", return_value=changes):
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf", "work_date": today,
                "next_steps": "补充回归检查", "publish_action": "save_only",
            })
            archive = self.client.get("/archive").get_data(as_text=True)
        self.assertEqual(response.status_code, 302)
        self.assertIn("最近代码改动", archive)
        self.assertIn("web/archive.html", archive)
        self.assertIn("补充回归检查", archive)
        with database_connection(self.database_path) as connection:
            content = connection.execute("SELECT content FROM published_items").fetchone()[0]
        self.assertIn("下一步计划", content)
        self.assertIn(format_code_changes(changes), content)

    def test_code_summary_alone_can_be_saved_and_sent(self):
        today = datetime.date.today().isoformat()
        changes = [{"summary": "更新 Python 逻辑", "path": "web/app.py",
                    "kind": "修改", "additions": 7, "deletions": 0,
                    "time": "2026-10-22T09:00+08:00"}]
        with patch("web.publisher_app.recent_code_changes", return_value=changes), \
             patch("web.publisher_app.smtp_settings_from_env", return_value="smtp"), \
             patch("web.publisher_app.write_local_smtp_config"), \
             patch("web.publisher_app.send_progress_email") as send_email:
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf", "work_date": today,
                "next_steps": "", "sender_email": "sender@example.com",
                "recipient_email": "recipient@example.com", "publish_action": "save_send",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(send_email.call_args.kwargs["recent_changes"], changes)
        with database_connection(self.database_path) as connection:
            content = connection.execute("SELECT content FROM published_items").fetchone()[0]
        self.assertIn("web/app.py", content)

    def test_unchecked_code_changes_are_not_published(self):
        today = datetime.date.today().isoformat()
        changes = [{"summary": "更新页面样式", "path": "web/static/style.css",
                    "kind": "修改", "additions": 24, "deletions": 3,
                    "time": "2026-10-22T09:00+08:00"}]
        with patch("web.publisher_app.recent_code_changes", return_value=changes), \
             patch("web.publisher_app.smtp_settings_from_env", return_value="smtp"), \
             patch("web.publisher_app.write_local_smtp_config"), \
             patch("web.publisher_app.send_progress_email") as send_email:
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf", "work_date": today,
                "next_steps": "补充回归检查", "sender_email": "sender@example.com",
                "recipient_email": "recipient@example.com", "publish_action": "save_send",
                "code_changes_present": "1",
                "code_changes_text": format_code_changes(changes),
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(send_email.call_args.kwargs["recent_changes"], [])
        with database_connection(self.database_path) as connection:
            content = connection.execute("SELECT content FROM published_items").fetchone()[0]
        self.assertIn("补充回归检查", content)
        self.assertNotIn("最近代码改动", content)

    def test_edited_code_change_wording_is_used_for_archive_and_email(self):
        today = datetime.date.today().isoformat()
        changes = [{"summary": "更新页面样式", "path": "web/static/style.css",
                    "kind": "修改", "additions": 24, "deletions": 3,
                    "time": "2026-10-22T09:00+08:00"}]
        wording = "重做了工作台样式，让代码改动可以随日报一起发送"
        with patch("web.publisher_app.recent_code_changes", return_value=changes), \
             patch("web.publisher_app.smtp_settings_from_env", return_value="smtp"), \
             patch("web.publisher_app.write_local_smtp_config"), \
             patch("web.publisher_app.send_progress_email") as send_email:
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf", "work_date": today,
                "next_steps": "", "sender_email": "sender@example.com",
                "recipient_email": "recipient@example.com", "publish_action": "save_send",
                "code_changes_present": "1", "include_code_changes": "1",
                "code_changes_text": wording,
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(send_email.call_args.kwargs["recent_changes_text"], wording)
        with database_connection(self.database_path) as connection:
            content = connection.execute("SELECT content FROM published_items").fetchone()[0]
        self.assertIn(wording, content)
        self.assertNotIn("web/static/style.css", content)

    def test_workbench_exposes_code_change_send_controls(self):
        changes = [{"summary": "更新 Python 逻辑", "path": "web/app.py",
                    "kind": "修改", "additions": 7, "deletions": 0,
                    "time": "2026-10-22T09:00+08:00"}]
        with self.client.session_transaction() as flask_session:
            flask_session["selected_source"] = "feishu"
        with patch("web.publisher_app.recent_code_changes", return_value=changes):
            page = self.client.get("/workbench").get_data(as_text=True)
        self.assertIn('name="include_code_changes"', page)
        self.assertIn('name="code_changes_text"', page)
        self.assertIn('name="code_changes_present"', page)
        self.assertIn("把这段改动附加到日报并发送", page)
        self.assertIn(format_code_changes(changes), page)

        with patch("web.publisher_app.recent_code_changes", return_value=changes):
            archive_page = self.client.get("/archive").get_data(as_text=True)
        self.assertNotIn('name="include_code_changes"', archive_page)

    def test_next_steps_alone_can_be_saved_without_code_changes(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]):
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf", "work_date": datetime.date.today().isoformat(),
                "next_steps": "明天整理测试", "publish_action": "save_only",
            })
        self.assertEqual(response.status_code, 302)
        with database_connection(self.database_path) as connection:
            content = connection.execute("SELECT content FROM published_items").fetchone()[0]
        self.assertIn("明天整理测试", content)
        self.assertNotIn("最近代码改动", content)

    def test_source_send_routes_forward_code_summary(self):
        changes = [{"summary": "调整页面模板", "path": "web/dashboard.html",
                    "kind": "修改", "additions": 3, "deletions": 2,
                    "time": "2026-10-22T09:00+08:00"}]
        today = datetime.date.today().isoformat()
        for source in ("github", "feishu"):
            with self.subTest(source=source):
                with self.client.session_transaction() as flask_session:
                    flask_session["selected_source"] = source
                    flask_session["progress_draft"] = {"work_date": today}
                    if source == "github":
                        flask_session["selected_repository"] = "owner/repo"
                    else:
                        flask_session["selected_feishu_chat_id"] = "oc_test"
                        flask_session["selected_feishu_chat_name"] = "测试群"
                with database_connection(self.database_path) as connection:
                    connection.execute(
                        "UPDATE publisher_users SET sender_email = ?, notification_email = ?, feishu_chat_id = ? WHERE id = 1",
                        ("sender@example.com", "recipient@example.com", "oc_test"),
                    )
                endpoint = "/github/send" if source == "github" else "/feishu/send"
                with patch("web.publisher_app.stream_for_publish", return_value=("数据流水", [{"tag": source}])), \
                     patch("web.publisher_app.smtp_settings_from_env", return_value="smtp"), \
                     patch("web.publisher_app.recent_code_changes", return_value=changes), \
                     patch("web.publisher_app.send_progress_email") as send_email:
                    response = self.client.post(endpoint, data={"csrf_token": "test-csrf"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(send_email.call_args.kwargs["recent_changes"], changes)

    def test_empty_summary_and_next_steps_do_not_publish(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]), \
             patch("web.publisher_app.send_progress_email") as send_email:
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf", "work_date": datetime.date.today().isoformat(),
                "next_steps": "", "publish_action": "save_send",
            })
        self.assertEqual(response.status_code, 302)
        send_email.assert_not_called()
        with database_connection(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM published_items").fetchone()[0]
        self.assertEqual(count, 0)

    def test_solved_problem_is_saved_as_its_own_section(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]):
            response = self.client.post("/publish", data={
                "csrf_token": "test-csrf",
                "work_date": datetime.date.today().isoformat(),
                "solved": "邮件发送失败时不再整页报错，改为逐条记录失败原因",
                "next_steps": "补齐回归测试",
                "publish_action": "save_only",
            })
        self.assertEqual(response.status_code, 302)
        with database_connection(self.database_path) as connection:
            content = connection.execute(
                "SELECT content FROM published_items"
            ).fetchone()[0]
        self.assertIn("解决的问题：\n邮件发送失败时不再整页报错", content)
        self.assertIn("下一步计划：\n补齐回归测试", content)
        self.assertLess(content.index("解决的问题"), content.index("下一步计划"))

    def test_solved_problem_alone_is_enough_to_publish(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]), \
             patch("web.publisher_app.smtp_settings_from_env", return_value="smtp"), \
             patch("web.publisher_app.send_progress_email") as send_email:
            self.client.post("/publish", data={
                "csrf_token": "test-csrf",
                "work_date": datetime.date.today().isoformat(),
                "sender_email": "3247026746@qq.com",
                "recipient_email": "3247026746@qq.com",
                "solved": "修好了归档页的时间显示",
                "publish_action": "save_send",
            })
        self.assertEqual(send_email.call_args.kwargs["solved"], "修好了归档页的时间显示")

    def test_hours_are_saved_into_report_content(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]):
            self.client.post("/publish", data={
                "csrf_token": "test-csrf",
                "work_date": datetime.date.today().isoformat(),
                "solved": "修好了归档页的时间显示",
                "hours": "6.5",
                "publish_action": "save_only",
            })
        with database_connection(self.database_path) as connection:
            content = connection.execute(
                "SELECT content FROM published_items"
            ).fetchone()[0]
        self.assertIn("投入工时：6.5 小时", content)

    def test_invalid_hours_block_publish(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]):
            self.client.post("/publish", data={
                "csrf_token": "test-csrf",
                "work_date": datetime.date.today().isoformat(),
                "solved": "修好了归档页的时间显示",
                "hours": "30",
                "publish_action": "save_only",
            })
        with database_connection(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM published_items"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_hours_are_passed_to_progress_email(self):
        with patch("web.publisher_app.recent_code_changes", return_value=[]), \
             patch("web.publisher_app.smtp_settings_from_env", return_value="smtp"), \
             patch("web.publisher_app.send_progress_email") as send_email:
            self.client.post("/publish", data={
                "csrf_token": "test-csrf",
                "work_date": datetime.date.today().isoformat(),
                "sender_email": "3247026746@qq.com",
                "recipient_email": "3247026746@qq.com",
                "solved": "修好了归档页的时间显示",
                "hours": "4",
                "publish_action": "save_send",
            })
        self.assertEqual(send_email.call_args.kwargs["hours"], "4")

    def test_workload_series_counts_archived_items_per_day(self):
        today = datetime.date.today()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with database_connection(self.database_path) as connection:
            for offset, count in ((0, 2), (2, 1)):
                for _ in range(count):
                    connection.execute(
                        """INSERT INTO published_items
                           (publisher_id, work_date, title, content, report_detail,
                            attachments, source_url, created_at)
                           VALUES (1, ?, '今日工作进展', '解决的问题：修复时间显示',
                                   '标准', '[]', NULL, ?)""",
                        ((today - datetime.timedelta(days=offset)).isoformat(), now),
                    )
        with database_connection(self.database_path) as connection:
            series = workload_series(connection, today, days=7)

        self.assertEqual(len(series["days"]), 7)
        by_date = {item["date"]: item["total"] for item in series["days"]}
        self.assertEqual(by_date[today.isoformat()], 2)
        self.assertEqual(
            by_date[(today - datetime.timedelta(days=2)).isoformat()], 1
        )
        self.assertEqual(series["total"], 3)
        self.assertEqual(series["peak"], 2)
        self.assertEqual(series["days"][-1]["percent"], 100)
        self.assertTrue(series["days"][-1]["is_today"])
        self.assertTrue(series["days"][-1]["is_peak"])


if __name__ == "__main__":
    unittest.main()
