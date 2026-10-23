"""简报模块与新版邮件版式的测试。"""

import unittest
from unittest.mock import patch

from web import email_notifications
from web.daily_brief import build_brief, build_mix, extract_tags, split_items


class SplitItemsTests(unittest.TestCase):
    def test_splits_lines_and_chinese_semicolons(self):
        items = split_items("优化日报页面布局；完善发布逻辑\n更新飞书模板")
        self.assertEqual(
            items, ["优化日报页面布局", "完善发布逻辑", "更新飞书模板"]
        )

    def test_strips_numbering_and_bullets(self):
        items = split_items("1. 完成数据统计\n- 测试自动发布\n② 优化移动端")
        self.assertEqual(items, ["完成数据统计", "测试自动发布", "优化移动端"])

    def test_ignores_blank_and_duplicate_items(self):
        items = split_items("\n\n完成统计\n完成统计\n ")
        self.assertEqual(items, ["完成统计"])

    def test_respects_limit(self):
        self.assertEqual(len(split_items("a1\nb2\nc3\nd4", limit=2)), 2)


class TagTests(unittest.TestCase):
    def test_extracts_frontend_and_automation_tags(self):
        tags = extract_tags("优化日报页面布局", "写一个自动拉取脚本")
        self.assertIn("#前端优化", tags)
        self.assertIn("#自动化", tags)

    def test_returns_empty_without_text(self):
        self.assertEqual(extract_tags("", None), [])


class MixTests(unittest.TestCase):
    def test_groups_changes_by_module(self):
        changes = [
            {"path": "web/publisher_app.py", "additions": 58, "deletions": 9},
            {"path": "web/app.py", "additions": 10, "deletions": 2},
            {"path": "web/static/style.css", "additions": 24, "deletions": 3},
        ]
        mix = build_mix(changes)
        labels = [row["label"] for row in mix["rows"]]
        self.assertEqual(labels[0], "Python 逻辑")
        self.assertIn("页面样式", labels)
        self.assertEqual(sum(row["percent"] for row in mix["rows"]), 100)
        self.assertIn("+92 行", mix["caption"])

    def test_falls_back_to_workload_sources(self):
        workload = {
            "days": [
                {"reports": 1, "tasks": 0, "collaborations": 0, "commits": 2, "messages": 0},
                {"reports": 0, "tasks": 0, "collaborations": 0, "commits": 1, "messages": 0},
            ]
        }
        mix = build_mix([], workload)
        labels = [row["label"] for row in mix["rows"]]
        self.assertEqual(labels, ["代码提交", "日报归档"])

    def test_returns_empty_when_nothing_to_compare(self):
        self.assertEqual(build_mix([], None)["rows"], [])


class BuildBriefTests(unittest.TestCase):
    def test_overview_adds_up(self):
        brief = build_brief(
            solved="优化日报页面布局；完善发布逻辑",
            next_steps="完成数据统计\n测试自动发布",
            recent_changes=[{"path": "web/app.py", "additions": 58, "deletions": 9}],
            hours="6.5",
        )
        overview = brief["overview"]
        self.assertEqual(overview["done"], 2)
        self.assertEqual(overview["doing"], 2)
        self.assertEqual(overview["tasks"], 4)
        self.assertEqual(overview["additions"], 58)
        self.assertEqual(overview["deletions"], 9)
        self.assertEqual(overview["net"], 49)
        self.assertEqual(overview["hours_label"], "6.5h")

    def test_hours_are_optional_and_validated(self):
        self.assertEqual(build_brief(hours="")["overview"]["hours_label"], "")
        self.assertEqual(build_brief(hours="30")["overview"]["hours"], None)
        self.assertEqual(build_brief(hours="abc")["overview"]["hours"], None)

    def test_summary_mentions_finished_work_and_risks(self):
        brief = build_brief(
            solved="修好了邮件图表渲染",
            problems="部分客户端不显示背景色",
            next_steps="补充回归测试",
        )
        self.assertIn("修好了邮件图表渲染", brief["summary"])
        self.assertIn("风险", brief["summary"])
        self.assertEqual(brief["insight"]["tone"], "tight")
        self.assertIn("部分客户端不显示背景色", brief["insight"]["advice"])

    def test_empty_brief_does_not_invent_content(self):
        brief = build_brief()
        self.assertEqual(brief["overview"]["tasks"], 0)
        self.assertEqual(brief["done_items"], [])
        self.assertEqual(brief["mix"], [])
        self.assertEqual(brief["tags"], [])

    def test_tight_state_when_only_risks_recorded(self):
        brief = build_brief(problems="接口偶发超时")
        self.assertEqual(brief["insight"]["state"], "节奏偏紧")

    def test_good_state_with_three_items_and_no_risk(self):
        brief = build_brief(solved="做了甲\n做了乙\n做了丙")
        self.assertEqual(brief["insight"]["state"], "状态良好")


class EmailSectionsTests(unittest.TestCase):
    def setUp(self):
        self.settings = email_notifications.SmtpSettings(
            host="smtp.qq.com",
            port=465,
            username="sender@qq.com",
            password="authorization-code",
            sender="sender@qq.com",
            secure="ssl",
        )

    def _render(self, **kwargs):
        with patch.object(email_notifications, "_send_message") as send_message:
            email_notifications.send_progress_email(
                self.settings,
                "recipient@example.com",
                "张三",
                "2026-10-22",
                kwargs.pop("finished", ""),
                kwargs.pop("problems", ""),
                kwargs.pop("next_steps", ""),
                **kwargs,
            )
        return send_message.call_args.args[2].get_content()

    def test_email_contains_all_new_blocks(self):
        changes = [
            {"summary": "更新 Python 逻辑", "path": "web/app.py", "kind": "修改",
             "additions": 58, "deletions": 9, "time": "2026-10-22T22:21+08:00"},
            {"summary": "更新页面样式", "path": "web/static/style.css", "kind": "修改",
             "additions": 24, "deletions": 3, "time": "2026-10-22T22:22+08:00"},
        ]
        body = self._render(
            solved="优化日报页面布局；完善发布逻辑",
            problems="部分客户端不显示背景色",
            next_steps="1. 完成数据统计\n2. 测试自动发布",
            recent_changes=changes,
            hours="6.5",
        )
        for block in ("今日概览", "今日总结", "今日完成", "问题与风险", "改动分布",
                      "明日计划", "工作洞察", "最近代码改动", "今日任务", "投入工时"):
            self.assertIn(block, body)
        self.assertIn("完成率 50%", body)
        self.assertIn("6.5h", body)
        self.assertIn("+58", body)

    def test_email_omits_hours_cell_when_not_filled(self):
        body = self._render(solved="修好了邮件图表渲染")
        self.assertIn("今日概览", body)
        self.assertNotIn("投入工时", body)

    def test_concise_detail_stays_short(self):
        body = self._render(
            solved="修好了邮件图表渲染",
            next_steps="补充回归测试",
            report_detail="简洁",
        )
        self.assertIn("今日进展", body)
        self.assertNotIn("今日概览", body)
        self.assertNotIn("工作洞察", body)

    def test_hours_rejected_when_out_of_range(self):
        body = self._render(solved="修好了邮件图表渲染", hours="99")
        self.assertNotIn("99h", body)


if __name__ == "__main__":
    unittest.main()
