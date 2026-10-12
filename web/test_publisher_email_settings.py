import tempfile
import unittest
import datetime
import io
import json
from pathlib import Path
from unittest.mock import patch

from web import email_notifications
from web.app import database_connection, initialize_database
from web.publisher_app import create_publisher_app
from web.publisher_auth import hash_password


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

        workbench = self.client.get("/")
        archive = self.client.get("/archive")
        github = self.client.get("/github")

        self.assertEqual(workbench.status_code, 200)
        self.assertIn("日报颗粒度", workbench.get_data(as_text=True))
        self.assertIn("添加照片", workbench.get_data(as_text=True))
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


if __name__ == "__main__":
    unittest.main()
