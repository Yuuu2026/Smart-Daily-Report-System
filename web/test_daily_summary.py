"""命令行日报总结模块的测试。"""

import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from web.daily_summary import (
    build_report,
    clean_subject,
    collect_changes,
    collect_commits,
    pick_plans,
    pick_problems,
)


def _commit(repo, message, filename="note.txt", text="hello\n"):
    path = Path(repo) / filename
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=测试", "-c", "user.email=test@example.com",
         "commit", "-q", "-m", message],
        cwd=repo, check=True, capture_output=True,
    )


class CleanSubjectTests(unittest.TestCase):
    def test_strips_conventional_prefix_and_scope(self):
        self.assertEqual(clean_subject("feat(web): 新增日报入口"), "新增日报入口")
        self.assertEqual(clean_subject("fix: 修复登录超时"), "修复登录超时")

    def test_keeps_plain_chinese_subject(self):
        self.assertEqual(
            clean_subject("邮件改为概览式版式：今日概览 + 自动总结"),
            "邮件改为概览式版式：今日概览 + 自动总结",
        )

    def test_drops_trailing_period(self):
        self.assertEqual(clean_subject("补充回归测试。"), "补充回归测试")


class CommitCollectionTests(unittest.TestCase):
    def test_reads_today_commits_with_numstat(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            _commit(repo, "feat: 第一件事", "a.txt", "one\n")
            today = datetime.date.today()
            commits = collect_commits(repo, today)
            self.assertEqual(len(commits), 1)
            self.assertEqual(commits[0]["subject"], "第一件事")
            self.assertEqual(commits[0]["files"], 1)
            self.assertEqual(commits[0]["paths"], ["a.txt"])
            self.assertGreaterEqual(commits[0]["additions"], 1)

    def test_other_day_returns_empty(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            _commit(repo, "feat: 今天的事", "a.txt", "one\n")
            past = datetime.date.today() - datetime.timedelta(days=30)
            self.assertEqual(collect_commits(repo, past), [])


class ChangeCollectionTests(unittest.TestCase):
    def test_filters_by_date(self):
        with tempfile.TemporaryDirectory() as repo:
            state = Path(repo) / ".claude" / "recent_code_changes.json"
            state.parent.mkdir(parents=True)
            state.write_text(
                json.dumps(
                    [
                        {"path": "web/app.py", "time": "2026-10-22T10:00+08:00"},
                        {"path": "web/x.py", "time": "2026-10-21T10:00+08:00"},
                    ]
                ),
                encoding="utf-8",
            )
            rows = collect_changes(repo, datetime.date(2026, 10, 22))
            self.assertEqual([item["path"] for item in rows], ["web/app.py"])

    def test_missing_cache_returns_empty(self):
        with tempfile.TemporaryDirectory() as repo:
            self.assertEqual(collect_changes(repo, datetime.date(2026, 10, 22)), [])


class BuildReportTests(unittest.TestCase):
    def _signals(self, **overrides):
        base = {
            "day": datetime.date(2026, 10, 22),
            "commits": [
                {
                    "sha": "a" * 40,
                    "short": "aaaaaaa",
                    "time": "2026-10-22 10:00",
                    "subject": "新增命令行日报入口",
                    "raw_subject": "feat: 新增命令行日报入口",
                    "body": "",
                    "files": 2,
                    "additions": 30,
                    "deletions": 5,
                    "paths": ["scripts/report_cli.py", "web/daily_summary.py"],
                }
            ],
            "changes": [],
            "prompts": [],
            "feishu": [],
        }
        base.update(overrides)
        return base

    def test_finished_comes_from_commits(self):
        report = build_report(self._signals())
        self.assertIn("- 新增命令行日报入口", report["finished"])
        self.assertEqual(report["sources"]["commits"], 1)
        self.assertEqual(report["sources"]["additions"], 30)

    def test_summary_mentions_commit_and_file_counts(self):
        report = build_report(self._signals())
        self.assertIn("今天提交 1 次", report["summary"])
        self.assertIn("改动 2 个文件", report["summary"])
        self.assertIn("+30 / −5", report["summary"])

    def test_internal_memory_commits_are_skipped(self):
        signals = self._signals()
        signals["commits"].append(
            {
                "sha": "b" * 40,
                "short": "bbbbbbb",
                "time": "2026-10-22 11:00",
                "subject": "记忆：记录日报口径",
                "raw_subject": "记忆：记录日报口径",
                "body": "",
                "files": 1,
                "additions": 5,
                "deletions": 0,
                "paths": ["MEMORY.md"],
            }
        )
        report = build_report(signals)
        self.assertNotIn("记忆", report["finished"])

    def test_fix_commit_goes_to_solved(self):
        signals = self._signals()
        signals["commits"][0]["raw_subject"] = "fix: 修复登录超时"
        signals["commits"][0]["subject"] = "修复登录超时"
        report = build_report(signals)
        self.assertIn("修复登录超时", report["solved"])
        self.assertNotIn("修复登录超时", report["finished"])

    def test_empty_day_reports_warning(self):
        signals = self._signals(commits=[], changes=[], prompts=[])
        report = build_report(signals)
        self.assertEqual(report["finished"], "")
        self.assertTrue(any("没有读到代码提交" in item for item in report["warnings"]))

    def test_uncommitted_changes_are_marked(self):
        signals = self._signals(
            commits=[],
            changes=[{"path": "web/publisher_app.py", "additions": 8, "deletions": 2}],
        )
        report = build_report(signals)
        self.assertIn("未提交", report["finished"])
        self.assertTrue(any("还没有提交" in item for item in report["warnings"]))

    def test_plans_from_prompts(self):
        signals = self._signals(
            prompts=[{"source": "workbuddy", "text": "下一步：补上 CLI 的回归测试。再说。"}]
        )
        report = build_report(signals)
        self.assertIn("补上 CLI 的回归测试", report["next_steps"])


class PlanAndProblemTests(unittest.TestCase):
    def test_pick_plans_keeps_only_plan_sentences(self):
        prompts = [
            {"text": "帮我加一个柱状图"},
            {"text": "接下来要补测试\n顺便改文档"},
        ]
        plans = pick_plans(prompts)
        self.assertEqual(plans, ["要补测试"])

    def test_pick_problems_skips_resolved_ones(self):
        prompts = [
            {"text": "登录报错已经解决了"},
            {"text": "邮件在手机上显示不对"},
        ]
        problems = pick_problems(prompts)
        self.assertEqual(problems, ["邮件在手机上显示不对"])

    def test_pick_plans_returns_empty_without_hint(self):
        self.assertEqual(pick_plans([{"text": "加一个柱状图"}]), [])


if __name__ == "__main__":
    unittest.main()
