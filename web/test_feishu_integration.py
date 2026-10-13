import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.app import database_connection, initialize_database
from web.publisher_app import create_publisher_app
from web.publisher_auth import hash_password


class FeishuIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "daily_report.db"
        initialize_database(self.database_path)
        with database_connection(self.database_path) as connection:
            connection.execute(
                """INSERT INTO publisher_users
                   (username, password_hash, display_name)
                   VALUES (?, ?, ?)""",
                ("zhangsan", hash_password("safe-password-123"), "张三"),
            )
        self.app = create_publisher_app(self.database_path, secret_key="test-secret")
        self.client = self.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session.update(
                {
                    "user_id": 1,
                    "username": "zhangsan",
                    "display_name": "张三",
                    "csrf_token": "test-csrf",
                }
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_login_redirects_to_source_selection(self):
        login_client = self.app.test_client()
        with login_client.session_transaction() as flask_session:
            flask_session["csrf_token"] = "login-csrf"

        response = login_client.post(
            "/login",
            data={
                "csrf_token": "login-csrf",
                "username": "zhangsan",
                "password": "safe-password-123",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/sources")

    def test_source_selection_page_offers_github_and_feishu(self):
        response = self.client.get("/sources")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("接入设置", page)
        self.assertIn("GitHub 仓库", page)
        self.assertIn("飞书群聊", page)

    def test_feishu_settings_select_chat_and_switch_workbench(self):
        chats = [
            {
                "chat_id": "oc_test_chat",
                "chat_name": "智能日报项目群",
                "description": "",
            }
        ]
        with patch("web.publisher_app.store_feishu_app_secret") as store_secret, patch(
            "web.publisher_app.list_feishu_chats",
            return_value=chats,
        ) as list_chats:
            response = self.client.post(
                "/feishu/settings",
                data={
                    "csrf_token": "test-csrf",
                    "action": "connect",
                    "app_id": "cli_test_app",
                    "app_secret": "test-app-secret",
                },
                follow_redirects=False,
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/feishu")
            store_secret.assert_called_once_with("cli_test_app", "test-app-secret")
            list_chats.assert_called_once_with("cli_test_app", "test-app-secret")

        response = self.client.post(
            "/feishu/settings",
            data={
                "csrf_token": "test-csrf",
                "action": "select",
                "app_id": "cli_test_app",
                "chat_id": "oc_test_chat",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/?publish_only=1")
        with database_connection(self.database_path) as connection:
            row = connection.execute(
                """SELECT feishu_app_id, feishu_chat_id, feishu_chat_name
                   FROM publisher_users WHERE id = 1"""
            ).fetchone()
        self.assertEqual(row["feishu_app_id"], "cli_test_app")
        self.assertEqual(row["feishu_chat_id"], "oc_test_chat")
        self.assertEqual(row["feishu_chat_name"], "智能日报项目群")

        workbench = self.client.get("/")
        workbench_text = workbench.get_data(as_text=True)
        self.assertEqual(workbench.status_code, 200)
        self.assertIn("当前群聊", workbench_text)
        self.assertIn("智能日报项目群", workbench_text)

    def test_feishu_sync_archives_collaboration_without_sending_email(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        message = {
            "message_id": "om_test_message",
            "chat_id": "oc_test_chat",
            "chat_name": "智能日报项目群",
            "sender_id": "ou_test_sender",
            "member_name": "李四",
            "msg_type": "text",
            "work_date": now.astimezone(
                datetime.timezone(datetime.timedelta(hours=8))
            ).date().isoformat(),
            "summary": "已确认飞书消息会进入日报协作记录",
            "created_at_utc": now.isoformat(timespec="seconds"),
            "collected_at_utc": now.isoformat(timespec="seconds"),
        }
        with database_connection(self.database_path) as connection:
            connection.execute(
                """UPDATE publisher_users
                   SET feishu_app_id = ?, feishu_chat_id = ?, feishu_chat_name = ?,
                       sender_email = ?, notification_email = ?
                   WHERE id = 1""",
                (
                    "cli_test_app",
                    "oc_test_chat",
                    "智能日报项目群",
                    "sender@example.com",
                    "recipient@example.com",
                ),
            )
        with self.client.session_transaction() as flask_session:
            flask_session.update(
                {
                    "selected_source": "feishu",
                    "feishu_app_id": "cli_test_app",
                    "selected_feishu_chat_id": "oc_test_chat",
                    "selected_feishu_chat_name": "智能日报项目群",
                }
            )

        with patch(
            "web.publisher_app.collect_feishu_recent_messages",
            return_value=("智能日报项目群", [message]),
        ) as collect, patch(
            "web.email_notifications.send_feishu_sync_email"
        ) as send_email:
            response = self.client.post(
                "/feishu/sync",
                data={"csrf_token": "test-csrf"},
                follow_redirects=False,
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/#activity-stream")
            collect.assert_called_once_with(
                "oc_test_chat",
                app_id="cli_test_app",
                chat_name="智能日报项目群",
            )
            send_email.assert_not_called()

        with database_connection(self.database_path) as connection:
            message_row = connection.execute(
                """SELECT message_id, chat_name, member_name, summary, record_id
                   FROM feishu_messages WHERE message_id = ?""",
                ("om_test_message",),
            ).fetchone()
            record_row = connection.execute(
                """SELECT work_date, category, member, channel, summary, extra_json
                   FROM records WHERE id = ?""",
                (message_row["record_id"],),
            ).fetchone()
            sync_row = connection.execute(
                """SELECT status, message_count, chat_name
                   FROM feishu_sync_runs ORDER BY id DESC LIMIT 1"""
            ).fetchone()

        self.assertEqual(message_row["chat_name"], "智能日报项目群")
        self.assertEqual(record_row["category"], "collaborations")
        self.assertEqual(record_row["member"], "李四")
        self.assertEqual(record_row["channel"], "智能日报项目群")
        self.assertIn("source_type", record_row["extra_json"])
        self.assertEqual(sync_row["status"], "succeeded")
        self.assertEqual(sync_row["message_count"], 1)

        feishu_page = self.client.get("/feishu")
        archive_page = self.client.get("/archive")
        feishu_text = feishu_page.get_data(as_text=True)
        archive_text = archive_page.get_data(as_text=True)
        self.assertIn("已确认飞书消息会进入日报协作记录", feishu_text)
        self.assertIn("飞书群聊", archive_text)
        self.assertIn("已确认飞书消息会进入日报协作记录", archive_text)

    def test_publish_works_after_selecting_feishu_without_github_repository(self):
        today = datetime.date.today().isoformat()
        with self.client.session_transaction() as flask_session:
            flask_session["selected_source"] = "feishu"
            flask_session["selected_feishu_chat_id"] = "oc_test_chat"
            flask_session["selected_feishu_chat_name"] = "智能日报项目群"

        response = self.client.post(
            "/publish",
            data={
                "csrf_token": "test-csrf",
                "work_date": today,
                "status": "已完成",
                "finished": "完成飞书群聊接入",
                "problems": "无阻塞问题",
                "next_steps": "验证群聊消息归档",
                "sender_email": "",
                "recipient_email": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with database_connection(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM published_items WHERE work_date = ?",
                (today,),
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
