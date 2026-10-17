import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from email.message import EmailMessage

from web import email_notifications


class SmtpSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / ".smtp.local.json"
        self.credentials_path = Path(self.temp_dir.name) / ".smtp.credentials.json"
        self.key_path = Path(self.temp_dir.name) / ".smtp.key"
        self.original_path = email_notifications.LOCAL_SMTP_CONFIG_PATH
        self.original_credentials_path = email_notifications.LOCAL_SMTP_CREDENTIALS_PATH
        self.original_key_path = email_notifications.LOCAL_SMTP_KEY_PATH
        email_notifications.LOCAL_SMTP_CONFIG_PATH = self.config_path
        email_notifications.LOCAL_SMTP_CREDENTIALS_PATH = self.credentials_path
        email_notifications.LOCAL_SMTP_KEY_PATH = self.key_path
        self.env_names = (
            "DAILY_REPORT_SMTP_HOST",
            "DAILY_REPORT_SMTP_PORT",
            "DAILY_REPORT_SMTP_SECURE",
            "DAILY_REPORT_SMTP_USERNAME",
            "DAILY_REPORT_SMTP_PASSWORD",
            "DAILY_REPORT_EMAIL_FROM",
        )
        self.original_env = {name: os.environ.get(name) for name in self.env_names}
        for name in self.env_names:
            os.environ.pop(name, None)

    def tearDown(self):
        email_notifications.LOCAL_SMTP_CONFIG_PATH = self.original_path
        email_notifications.LOCAL_SMTP_CREDENTIALS_PATH = self.original_credentials_path
        email_notifications.LOCAL_SMTP_KEY_PATH = self.original_key_path
        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp_dir.cleanup()

    def write_config(self, data):
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

    def test_reads_local_config_and_keyring_password(self):
        self.write_config(
            {
                "host": "smtp.qq.com",
                "port": 465,
                "secure": "ssl",
                "username": "sender@example.com",
                "sender": "daily@example.com",
            }
        )
        with patch.object(
            email_notifications,
            "_smtp_password_from_keyring",
            return_value="authorization-code",
        ):
            settings = email_notifications.smtp_settings_from_env()

        self.assertEqual(settings.host, "smtp.qq.com")
        self.assertEqual(settings.port, 465)
        self.assertEqual(settings.secure, "ssl")
        self.assertEqual(settings.username, "sender@example.com")
        self.assertEqual(settings.password, "authorization-code")
        self.assertEqual(settings.sender, "daily@example.com")

    def test_environment_overrides_local_config(self):
        self.write_config({"host": "smtp.example.com", "username": "local@example.com"})
        os.environ.update(
            {
                "DAILY_REPORT_SMTP_HOST": "smtp.qq.com",
                "DAILY_REPORT_SMTP_PORT": "465",
                "DAILY_REPORT_SMTP_SECURE": "ssl",
                "DAILY_REPORT_SMTP_USERNAME": "sender@example.com",
                "DAILY_REPORT_SMTP_PASSWORD": "env-code",
                "DAILY_REPORT_EMAIL_FROM": "daily@example.com",
            }
        )

        settings = email_notifications.smtp_settings_from_env()

        self.assertEqual(settings.host, "smtp.qq.com")
        self.assertEqual(settings.username, "sender@example.com")
        self.assertEqual(settings.password, "env-code")
        self.assertEqual(settings.sender, "daily@example.com")

    def test_sender_email_selects_provider_settings(self):
        self.write_config(
            {
                "host": "smtp.example.com",
                "port": 587,
                "secure": "starttls",
                "username": "local@example.com",
                "sender": "local@example.com",
            }
        )
        with patch.object(
            email_notifications,
            "_smtp_password_from_keyring",
            return_value="authorization-code",
        ):
            settings = email_notifications.smtp_settings_from_env("sender@qq.com")

        self.assertEqual(settings.host, "smtp.qq.com")
        self.assertEqual(settings.port, 465)
        self.assertEqual(settings.secure, "ssl")
        self.assertEqual(settings.username, "sender@qq.com")
        self.assertEqual(settings.sender, "sender@qq.com")

    def test_write_local_smtp_config_matches_provider(self):
        email_notifications.write_local_smtp_config("sender@qq.com")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["host"], "smtp.qq.com")
        self.assertEqual(saved["port"], 465)
        self.assertEqual(saved["secure"], "ssl")
        self.assertEqual(saved["username"], "sender@qq.com")
        self.assertEqual(saved["sender"], "sender@qq.com")

    def test_missing_authorization_code_is_reported(self):
        self.write_config({"host": "smtp.qq.com", "username": "sender@example.com"})
        with patch.object(email_notifications, "_smtp_password_from_keyring", return_value=""):
            with self.assertRaisesRegex(email_notifications.EmailNotificationError, "授权码"):
                email_notifications.smtp_settings_from_env()

    def test_keyring_failure_falls_back_to_encrypted_local_file(self):
        with patch(
            "keyring.set_password",
            side_effect=OSError(1312, "指定的登录会话不存在。"),
        ), patch("keyring.get_password", return_value=""):
            email_notifications.store_smtp_password("sender@qq.com", "authorization-code")
            password = email_notifications._smtp_password_from_keyring("sender@qq.com")

        stored_text = self.credentials_path.read_text(encoding="utf-8")
        self.assertEqual(password, "authorization-code")
        self.assertNotIn("authorization-code", stored_text)
        self.assertIn("sender@qq.com", stored_text)

    def test_settings_use_local_password_when_keyring_is_empty(self):
        self.write_config({"host": "smtp.qq.com", "port": 465, "secure": "ssl"})
        with patch("keyring.set_password", side_effect=OSError(1312)), patch(
            "keyring.get_password", return_value=""
        ):
            email_notifications.store_smtp_password("sender@qq.com", "authorization-code")
            settings = email_notifications.smtp_settings_from_env("sender@qq.com")

        self.assertEqual(settings.host, "smtp.qq.com")
        self.assertEqual(settings.port, 465)
        self.assertEqual(settings.secure, "ssl")
        self.assertEqual(settings.username, "sender@qq.com")
        self.assertEqual(settings.password, "authorization-code")

    def test_successful_keyring_write_does_not_create_local_file(self):
        with patch("keyring.set_password") as set_password:
            email_notifications.store_smtp_password("sender@qq.com", "authorization-code")

        set_password.assert_called_once()
        self.assertFalse(self.credentials_path.exists())

    def test_non_ascii_authorization_code_is_rejected_before_storage(self):
        with self.assertRaisesRegex(
            email_notifications.EmailNotificationError,
            "授权码格式不正确",
        ):
            email_notifications.store_smtp_password("sender@qq.com", "授权码")

        self.assertFalse(self.credentials_path.exists())

    def test_non_ascii_saved_authorization_code_is_rejected_when_loading(self):
        self.write_config({"host": "smtp.qq.com", "port": 465, "secure": "ssl"})
        with patch.object(
            email_notifications,
            "_smtp_password_from_keyring",
            return_value="授权码",
        ):
            with self.assertRaisesRegex(
                email_notifications.EmailNotificationError,
                "授权码格式不正确",
            ):
                email_notifications.smtp_settings_from_env("sender@qq.com")

    def test_unicode_login_failure_becomes_friendly_email_error(self):
        settings = email_notifications.SmtpSettings(
            host="smtp.qq.com",
            port=465,
            username="sender@qq.com",
            password="authorization-code",
            sender="sender@qq.com",
            secure="ssl",
        )
        message = EmailMessage()
        message["To"] = "recipient@example.com"
        unicode_error = UnicodeEncodeError(
            "ascii", "非ASCII授权码", 0, 7, "ordinal not in range"
        )
        with patch.object(email_notifications.smtplib, "SMTP_SSL") as smtp_ssl:
            smtp = smtp_ssl.return_value.__enter__.return_value
            smtp.login.side_effect = unicode_error
            with self.assertRaisesRegex(
                email_notifications.EmailNotificationError,
                "邮件发送失败",
            ):
                email_notifications._send_message(settings, "recipient@example.com", message)

    def test_progress_email_uses_structured_daily_report_format(self):
        settings = email_notifications.SmtpSettings(
            host="smtp.qq.com",
            port=465,
            username="sender@qq.com",
            password="authorization-code",
            sender="sender@qq.com",
            secure="ssl",
        )
        with patch.object(email_notifications, "_send_message") as send_message:
            email_notifications.send_progress_email(
                settings,
                "recipient@example.com",
                "张三",
                "2026-10-11",
                "完成日报结构化渲染",
                "历史数据存在旧标签",
                "补充证据链接",
                status="已完成",
                source_url="https://example.com/report",
            )

        message = send_message.call_args.args[2]
        self.assertEqual(message["Subject"], "研一 张三 2026-10-11 智能日报")
        content = message.get_content()
        self.assertIn("智能日报系统自动生成", content)
        self.assertIn("- [成员发布] 完成日报结构化渲染", content)
        self.assertIn("- [成员发布] 历史数据存在旧标签", content)
        self.assertIn("- [成员发布] 补充证据链接", content)
        self.assertNotIn("来源链接", content)

    def test_github_sync_email_uses_structured_daily_report_format(self):
        settings = email_notifications.SmtpSettings(
            host="smtp.qq.com",
            port=465,
            username="sender@qq.com",
            password="authorization-code",
            sender="sender@qq.com",
            secure="ssl",
        )
        commit = {
            "sha": "1234567890abcdef",
            "member_name": "张三",
            "message": "重构日报生成结构\n\n调整正文粒度和证据附录",
            "additions": 120,
            "deletions": 30,
            "changed_files": 8,
            "commit_url": "https://github.com/org/repo/commit/1234567890abcdef",
        }
        with patch.object(email_notifications, "_send_message") as send_message:
            email_notifications.send_github_sync_email(
                settings,
                "recipient@example.com",
                "张三",
                "org/repo",
                "succeeded",
                1,
                "2026-10-11T08:00:00+00:00",
                "2026-10-11T08:01:00+00:00",
                commits=[commit],
            )

        content = send_message.call_args.args[2].get_content()
        self.assertIn("智能日报系统自动生成", content)
        self.assertIn("数据窗口", content)
        self.assertIn("最近 24 小时", content)
        self.assertIn("今日概览", content)
        self.assertIn("- [系统汇总] 完成 GitHub 同步", content)
        self.assertIn("- [GitHub] 张三：重构日报生成结构", content)

    def test_github_sync_failure_email_keeps_details_in_evidence(self):
        settings = email_notifications.SmtpSettings(
            host="smtp.qq.com",
            port=465,
            username="sender@qq.com",
            password="authorization-code",
            sender="sender@qq.com",
            secure="ssl",
        )
        with patch.object(email_notifications, "_send_message") as send_message:
            email_notifications.send_github_sync_email(
                settings,
                "recipient@example.com",
                "张三",
                "org/repo",
                "failed",
                0,
                "2026-10-11T08:00:00+00:00",
                "2026-10-11T08:01:00+00:00",
                error_message="GitHub API 返回 502 Bad Gateway",
            )

        content = send_message.call_args.args[2].get_content()
        self.assertIn("- [GitHub] 同步失败：GitHub API 返回 502 Bad Gateway", content)


if __name__ == "__main__":
    unittest.main()
