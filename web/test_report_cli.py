"""命令行日报入口的测试。"""

import datetime
import io
import unittest
from unittest.mock import MagicMock, patch

from scripts.report_cli import (
    ask_send_target,
    build_content,
    configured_recipients,
    deliver,
    load_config,
    parse_args,
    parse_recipients,
    smtp_settings_for,
)
from web import email_notifications


class ContentTests(unittest.TestCase):
    def test_sections_follow_fixed_order(self):
        report = {
            "hours": "6.5",
            "solved": "- 修复登录超时",
            "finished": "- 新增命令行入口",
            "problems": "",
            "next_steps": "- 补测试",
        }
        content = build_content(report)
        self.assertTrue(content.startswith("工作状态：已完成"))
        self.assertIn("投入工时：6.5 小时", content)
        self.assertIn("解决的问题：\n- 修复登录超时", content)
        self.assertLess(content.index("解决的问题"), content.index("完成事项"))
        self.assertLess(content.index("完成事项"), content.index("下一步计划"))
        self.assertNotIn("问题与风险", content)

    def test_returns_empty_without_any_section(self):
        report = {"solved": "", "finished": "", "problems": "", "next_steps": ""}
        self.assertEqual(build_content(report), "")


class RecipientTests(unittest.TestCase):
    def test_reads_three_slots_and_dedupes(self):
        user = {
            "notification_email": "a@x.com",
            "notification_email_2": "b@y.com",
            "notification_email_3": "a@x.com",
        }
        self.assertEqual(configured_recipients(user), ["a@x.com", "b@y.com"])

    def test_skips_blank_slots(self):
        user = {"notification_email": "a@x.com", "notification_email_2": "", "notification_email_3": None}
        self.assertEqual(configured_recipients(user), ["a@x.com"])


class ArgsTests(unittest.TestCase):
    def test_defaults_to_today(self):
        self.assertEqual(parse_args([]).date, datetime.date.today())

    def test_parses_recipients_and_flags(self):
        args = parse_args(["--date", "2026-10-22", "--to", "a@x.com", "--yes"])
        self.assertEqual(args.date, datetime.date(2026, 10, 22))
        self.assertEqual(args.to, "a@x.com")
        self.assertTrue(args.yes)

    def test_rejects_bad_date(self):
        with self.assertRaises(SystemExit):
            parse_args(["--date", "2026/10/22"])


class DeliverTests(unittest.TestCase):
    def _report(self):
        return {
            "date": "2026-10-22",
            "finished": "- 新增命令行入口",
            "problems": "",
            "next_steps": "- 补测试",
            "solved": "",
            "workload": None,
            "hours": "",
        }

    def _user(self):
        return {"id": 1, "display_name": "张三", "sender_email": "sender@qq.com"}

    def test_contains_failed_recipient_without_stopping(self):
        with patch("scripts.report_cli.email_notifications.smtp_settings_from_env", return_value=object()), patch(
            "scripts.report_cli.email_notifications.send_progress_email",
            side_effect=[None, email_notifications.EmailNotificationError("smtp 拒绝")],
        ), patch("scripts.report_cli.database_connection", MagicMock()), patch(
            "scripts.report_cli.record_email_delivery"
        ) as record:
            sent, failed = deliver(
                self._user(), ["a@x.com", "b@y.com"], self._report(), {"feishu": []}, False, []
            )
        self.assertEqual(sent, ["a@x.com"])
        self.assertEqual(failed, [("b@y.com", "smtp 拒绝")])
        self.assertEqual(record.call_count, 2)

    def test_passes_feishu_stream_when_selected(self):
        signals = {
            "feishu": [
                {"chat_name": "智能日报系统", "member": "李四", "summary": "明天联调"}
            ]
        }
        with patch("scripts.report_cli.email_notifications.smtp_settings_from_env", return_value=object()), patch(
            "scripts.report_cli.email_notifications.send_progress_email"
        ) as sender, patch("scripts.report_cli.database_connection", MagicMock()), patch(
            "scripts.report_cli.record_email_delivery"
        ):
            deliver(self._user(), ["a@x.com"], self._report(), signals, True, [])
        kwargs = sender.call_args.kwargs
        self.assertEqual(kwargs["stream_label"], "智能日报系统")
        self.assertEqual(kwargs["stream_items"][0]["member"], "李四")

    def test_skips_feishu_stream_when_declined(self):
        signals = {"feishu": [{"chat_name": "群", "member": "李四", "summary": "明天联调"}]}
        with patch("scripts.report_cli.email_notifications.smtp_settings_from_env", return_value=object()), patch(
            "scripts.report_cli.email_notifications.send_progress_email"
        ) as sender, patch("scripts.report_cli.database_connection", MagicMock()), patch(
            "scripts.report_cli.record_email_delivery"
        ):
            deliver(self._user(), ["a@x.com"], self._report(), signals, False, [])
        self.assertEqual(sender.call_args.kwargs["stream_items"], [])


class ConfigFileTests(unittest.TestCase):
    """scripts/report_config.py 是手改配置的唯一入口，必须能容错。"""

    def _write(self, body):
        import tempfile
        from pathlib import Path

        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        )
        handle.write(body)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_missing_file_falls_back_to_defaults(self):
        config = load_config("Z:/definitely/not/here/report_config.py")
        self.assertEqual(config["RECIPIENTS"], ())
        self.assertEqual(config["REPORT_DETAIL"], "标准")
        self.assertFalse(config["AUTO_SEND"])

    def test_reads_recipients_and_flags(self):
        path = self._write(
            'USER = "songhaoyu"\n'
            'RECIPIENTS = ["a@x.com", "b@y.com"]\n'
            'REPORT_DETAIL = "详细"\n'
            "AUTO_SEND = True\n"
            "INCLUDE_FEISHU = False\n"
        )
        config = load_config(path)
        self.assertEqual(config["USER"], "songhaoyu")
        self.assertEqual(config["RECIPIENTS"], ("a@x.com", "b@y.com"))
        self.assertEqual(config["REPORT_DETAIL"], "详细")
        self.assertTrue(config["AUTO_SEND"])
        self.assertFalse(config["INCLUDE_FEISHU"])

    def test_comma_string_recipients_are_split(self):
        path = self._write('RECIPIENTS = "a@x.com, b@y.com"\n')
        self.assertEqual(load_config(path)["RECIPIENTS"], ("a@x.com", "b@y.com"))

    def test_unknown_detail_falls_back_to_standard(self):
        path = self._write('REPORT_DETAIL = "超长"\n')
        self.assertEqual(load_config(path)["REPORT_DETAIL"], "标准")

    def test_syntax_error_does_not_raise(self):
        path = self._write("RECIPIENTS = [\n")
        with patch("builtins.print"):
            config = load_config(path)
        self.assertEqual(config["RECIPIENTS"], ())

    def test_bundled_config_is_loadable(self):
        config = load_config()
        self.assertIn(config["REPORT_DETAIL"], {"简洁", "标准", "详细"})
        self.assertIsInstance(config["RECIPIENTS"], tuple)


class SmtpConfigTests(unittest.TestCase):
    """配置文件里显式写 SMTP 时，应绕过本机 .smtp.local.json。"""

    USER = {"sender_email": "me@qq.com", "display_name": "张三"}

    def _config(self, **overrides):
        base = {
            "SMTP_HOST": "",
            "SMTP_PORT": "",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "SMTP_SECURE": "",
        }
        base.update(overrides)
        return base

    def test_blank_host_uses_local_config(self):
        with patch(
            "scripts.report_cli.email_notifications.smtp_settings_from_env",
            return_value="local",
        ) as loader:
            self.assertEqual(smtp_settings_for(self.USER, self._config()), "local")
        loader.assert_called_once_with("me@qq.com")

    def test_explicit_host_builds_settings(self):
        config = self._config(
            SMTP_HOST="smtp.qq.com", SMTP_PASSWORD="auth-code", SMTP_PORT="465"
        )
        settings = smtp_settings_for(self.USER, config)
        self.assertEqual(settings.host, "smtp.qq.com")
        self.assertEqual(settings.port, 465)
        self.assertEqual(settings.secure, "ssl")
        self.assertEqual(settings.username, "me@qq.com")
        self.assertEqual(settings.sender, "me@qq.com")

    def test_port_587_defaults_to_starttls(self):
        config = self._config(
            SMTP_HOST="smtp.example.com", SMTP_PASSWORD="x", SMTP_PORT="587"
        )
        self.assertEqual(smtp_settings_for(self.USER, config).secure, "starttls")

    def test_missing_password_is_reported(self):
        config = self._config(SMTP_HOST="smtp.example.com")
        with self.assertRaises(email_notifications.EmailNotificationError):
            smtp_settings_for(self.USER, config)


class SendTargetTests(unittest.TestCase):
    """一步确认：回车用默认、填邮箱替换、n 取消。"""

    def _ask(self, typed, recipients=("me@qq.com",)):
        with patch("builtins.input", return_value=typed), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            return ask_send_target(list(recipients))

    def test_enter_keeps_default_recipients(self):
        self.assertEqual(self._ask(""), ["me@qq.com"])

    def test_typed_email_replaces_default(self):
        self.assertEqual(self._ask("a@x.com, b@y.com"), ["a@x.com", "b@y.com"])

    def test_cancel_words_return_none(self):
        for word in ("n", "N", "取消", "不发"):
            with self.subTest(word=word):
                self.assertIsNone(self._ask(word))

    def test_empty_default_and_empty_input_returns_empty(self):
        self.assertEqual(self._ask("", recipients=()), [])


class RecipientParsingTests(unittest.TestCase):
    def test_comma_and_space_separated_are_split(self):
        self.assertEqual(
            parse_recipients("a@x.com, b@y.com  c@z.com"),
            ["a@x.com", "b@y.com", "c@z.com"],
        )

    def test_chinese_comma_is_accepted(self):
        self.assertEqual(parse_recipients("a@x.com，b@y.com"), ["a@x.com", "b@y.com"])

    def test_invalid_address_is_rejected(self):
        with self.assertRaises(email_notifications.EmailNotificationError):
            parse_recipients("a@x.com, not-an-email")

    def test_multiple_recipients_allowed_on_command_line(self):
        args = parse_args(["--to", "a@x.com,b@y.com", "--yes"])
        self.assertEqual(args.to, "a@x.com,b@y.com")


if __name__ == "__main__":
    unittest.main()
