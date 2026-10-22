import datetime
import tempfile
import unittest
from pathlib import Path

from report_core import (
    compact_progress_text,
    parse_progress_sections,
    render_structured_report,
)
from report_quality import inspect_progress_text, quality_warnings
from web.app import build_report_text, database_connection, initialize_database


class ReportGenerationTests(unittest.TestCase):
    def test_parse_progress_sections_supports_old_labels(self):
        sections = parse_progress_sections(
            "今天完成了什么：\n完成日报页面重构\n\n"
            "遇到了什么问题：\nSMTP 授权码保存失败\n\n"
            "下一步准备做什么：\n验证正式发送链路"
        )

        self.assertEqual(sections["finished"], "完成日报页面重构")
        self.assertEqual(sections["problems"], "SMTP 授权码保存失败")
        self.assertEqual(sections["next_steps"], "验证正式发送链路")

    def test_parse_progress_sections_supports_code_summary(self):
        sections = parse_progress_sections(
            "工作状态：已完成\n\n"
            "下一步计划：\n整理测试\n\n"
            "最近代码改动：\n更新 Python 逻辑 · web/app.py\n"
            "调整页面模板 · web/dashboard.html"
        )
        self.assertEqual(sections["next_steps"], "整理测试")
        self.assertEqual(
            sections["code_changes"],
            "更新 Python 逻辑 · web/app.py\n调整页面模板 · web/dashboard.html",
        )
        self.assertEqual(sections["finished"], "")

    def test_compact_progress_text_truncates_long_details(self):
        long_text = "完成" + "继续补充很多实现细节" * 10
        compact = compact_progress_text(long_text)

        self.assertLessEqual(len(compact), 80)
        self.assertTrue(compact.endswith("……"))

    def test_quality_warnings_cover_coarse_and_detailed_entries(self):
        warnings = quality_warnings(
            {
                "finished": "修好了",
                "problems": "",
                "next_steps": "修复" + "，同时继续补充新的验证细节" * 6,
            }
        )
        joined = "\n".join(warnings)

        self.assertIn("内容偏粗", joined)
        self.assertIn("内容偏细", joined)

    def test_render_structured_report_has_labels_and_metadata(self):
        groups = {
            "张三": {
                "commits": [],
                "tasks": [],
                "collaborations": [
                    {"member": "张三", "channel": "课程群", "summary": "和老师确认日报需要突出自动生成依据"}
                ],
            }
        }
        published = [
            {
                "member": "李四",
                "title": "日报结构化",
                "content": (
                    "工作状态：已完成\n\n完成事项：完成日报正文分层\n\n"
                    "问题与风险：历史数据存在旧标签\n\n下一步计划：补充证据链接"
                ),
                "source_url": "https://example.com/report",
                "created_at": "2026-10-11T02:00:00+00:00",
            }
        ]
        github = [
            {
                "member": "王五",
                "repository": "owner/repo",
                "sha": "abcdef1234567890",
                "message": "重构日报生成逻辑",
                "additions": 120,
                "deletions": 18,
                "changed_files": 5,
                "commit_url": "https://example.com/commit",
            }
        ]
        report = render_structured_report(
            groups,
            "2026-10-11",
            published_rows=published,
            github_rows=github,
            generated_at=datetime.datetime(2026, 10, 11, 10, 0),
        )

        self.assertIn("系统自动生成 · 生成时间：2026-10-11 10:00", report)
        self.assertIn("数据窗口：2026-10-11 00:00 - 23:59", report)
        self.assertIn("- [GitHub] owner/repo：重构日报生成逻辑", report)
        self.assertIn("- [成员发布] 已完成：完成日报正文分层", report)
        self.assertIn("- [协作记录] 课程群：", report)
        self.assertNotIn("## 证据附录", report)

    def test_build_report_text_uses_database_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "daily_report.db"
            initialize_database(database_path)
            with database_connection(database_path) as connection:
                connection.execute(
                    """INSERT INTO publisher_users
                       (username, password_hash, display_name, sender_email, notification_email)
                       VALUES ('zhangsan', 'hash', '张三', '', '')"""
                )
                connection.execute(
                    """INSERT INTO published_items
                       (publisher_id, work_date, title, content, source_url, created_at)
                       VALUES (1, '2026-10-11', '日报结构化',
                               '工作状态：已完成\n\n完成事项：完成日报结构化渲染\n\n问题与风险：无\n\n下一步计划：补充测试',
                               'https://example.com/report', '2026-10-11T02:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO github_commits
                       (repository, sha, member_name, work_date, message, additions,
                        deletions, changed_files, commit_url, committed_at_utc,
                        collected_at_utc)
                       VALUES ('owner/repo', 'abcdef1234567890', '李四', '2026-10-11',
                               '同步日报数据', 20, 2, 2, 'https://example.com/commit',
                               '2026-10-11T03:00:00+00:00', '2026-10-11T04:00:00+00:00')"""
                )

            report = build_report_text("2026-10-11", database_path)

            self.assertIn("[成员发布] 已完成：完成日报结构化渲染", report)
            self.assertIn("[GitHub] owner/repo：同步日报数据", report)
            self.assertNotIn("## 证据附录", report)
            self.assertNotIn("查看提交", report)


if __name__ == "__main__":
    unittest.main()
