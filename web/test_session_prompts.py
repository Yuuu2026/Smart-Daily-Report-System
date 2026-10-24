"""会话提示词采集的测试。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from web.session_prompts import (
    clean_text,
    claude_slug,
    collect_prompts,
    workbuddy_slug,
)


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class CleanTextTests(unittest.TestCase):
    def test_strips_system_reminder_but_keeps_question(self):
        raw = (
            "<system-reminder data-role=\"user-context\">\n内部提示\n"
            "</system-reminder>\n<user_query>加一个柱状图</user_query>"
        )
        self.assertEqual(clean_text(raw), "加一个柱状图")

    def test_removes_fenced_code(self):
        self.assertEqual(clean_text("看看这段\n```py\nprint(1)\n```\n行不行"), "看看这段 行不行")

    def test_returns_empty_for_blank(self):
        self.assertEqual(clean_text("   "), "")


class SlugTests(unittest.TestCase):
    def test_workbuddy_slug_keeps_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "智能日报系统设计"
            root.mkdir()
            slug = workbuddy_slug(root)
        self.assertIn("智能日报系统设计", slug)
        self.assertNotIn(":", slug)

    def test_claude_slug_masks_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "智能日报系统设计"
            root.mkdir()
            slug = claude_slug(root)
        self.assertNotIn("智能", slug)
        self.assertIn("---", slug)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "项目"
        self.root.mkdir()
        self.home = base / "home"
        self.day = datetime.date(2026, 10, 22)
        stamp = int(
            datetime.datetime(2026, 10, 22, 10, 30, tzinfo=datetime.timezone.utc).timestamp()
            * 1000
        )
        self.stamp = stamp

    def tearDown(self):
        self.tmp.cleanup()

    def _workbuddy_file(self, rows):
        path = self.home / ".workbuddy" / "projects" / workbuddy_slug(self.root) / "s.jsonl"
        _write(path, rows)
        return path

    def _claude_file(self, rows):
        path = self.home / ".claude" / "projects" / claude_slug(self.root) / "s.jsonl"
        _write(path, rows)
        return path

    def test_reads_workbuddy_format(self):
        self._workbuddy_file(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "把日报改成命令行发送"}],
                    "timestamp": self.stamp,
                }
            ]
        )
        rows = collect_prompts(self.root, day=self.day, home=self.home)
        self.assertEqual([item["text"] for item in rows], ["把日报改成命令行发送"])
        self.assertEqual(rows[0]["source"], "workbuddy")

    def test_reads_claude_format(self):
        self._claude_file(
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": "补一个测试"}]},
                    "timestamp": "2026-10-22T02:00:00Z",
                    "cwd": str(self.root),
                }
            ]
        )
        rows = collect_prompts(self.root, day=self.day, home=self.home)
        self.assertEqual([item["text"] for item in rows], ["补一个测试"])
        self.assertEqual(rows[0]["source"], "claude")

    def test_ignores_tool_results_and_noise(self):
        self._workbuddy_file(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "继续"}],
                    "timestamp": self.stamp,
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "$ git status"}],
                    "timestamp": self.stamp,
                },
            ]
        )
        self._claude_file(
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "输出"}],
                    },
                    "timestamp": "2026-10-22T02:00:00Z",
                }
            ]
        )
        self.assertEqual(collect_prompts(self.root, day=self.day, home=self.home), [])

    def test_dedupes_repeated_prompts(self):
        self._workbuddy_file(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "加上飞书消息"}],
                    "timestamp": self.stamp,
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "加上飞书消息"}],
                    "timestamp": self.stamp + 1000,
                },
            ]
        )
        rows = collect_prompts(self.root, day=self.day, home=self.home)
        self.assertEqual(len(rows), 1)

    def test_ignores_other_days(self):
        self._workbuddy_file(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "昨天说的话"}],
                    "timestamp": self.stamp - 3 * 86400 * 1000,
                }
            ]
        )
        self.assertEqual(collect_prompts(self.root, day=self.day, home=self.home), [])

    def test_ignores_compressed_history_injection(self):
        self._workbuddy_file(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<conversation_history_summary>历史摘要</conversation_history_summary>",
                        }
                    ],
                    "timestamp": self.stamp,
                }
            ]
        )
        self.assertEqual(collect_prompts(self.root, day=self.day, home=self.home), [])

    def test_limit_keeps_latest(self):
        rows = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"第{i}条需求内容"}],
                "timestamp": self.stamp + i * 1000,
            }
            for i in range(6)
        ]
        self._workbuddy_file(rows)
        picked = collect_prompts(self.root, day=self.day, home=self.home, limit=2)
        self.assertEqual([item["text"] for item in picked], ["第4条需求内容", "第5条需求内容"])


if __name__ == "__main__":
    unittest.main()
