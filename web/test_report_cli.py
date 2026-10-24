"""命令行日报入口的测试。"""

import datetime
import unittest
from unittest.mock import MagicMock, patch

from scripts.report_cli import (
    build_content,
    configured_recipients,
    deliver,
    parse_args,
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


if __name__ == "__main__":
    unittest.main()
