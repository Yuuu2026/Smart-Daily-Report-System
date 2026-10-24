"""发送前编辑草稿的测试：添加、修改、删除、改工时。"""

import unittest

from scripts.report_editor import (
    apply_items,
    collect_items,
    edit_report,
    format_items,
    join_items,
    parse_hours,
    split_items,
)


def make_report():
    return {
        "finished": "- 甲\n- 乙",
        "solved": "- 丙",
        "problems": "",
        "next_steps": "",
        "hours": "",
    }


class Reader:
    """按顺序吐答案；答案用完返回空串，避免死循环。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""


class SplitAndJoinTests(unittest.TestCase):
    def test_dash_prefix_is_stripped(self):
        self.assertEqual(split_items("- 甲\n- 乙"), ["甲", "乙"])

    def test_plain_lines_are_accepted(self):
        self.assertEqual(split_items("甲\n乙"), ["甲", "乙"])

    def test_blank_lines_are_dropped(self):
        self.assertEqual(split_items("- 甲\n\n- 乙\n"), ["甲", "乙"])

    def test_empty_text_gives_empty_list(self):
        self.assertEqual(split_items(""), [])
        self.assertEqual(split_items(None), [])

    def test_join_restores_dash_format(self):
        self.assertEqual(join_items(["甲", "乙"]), "- 甲\n- 乙")
        self.assertEqual(join_items([]), "")


class CollectTests(unittest.TestCase):
    def test_numbering_is_continuous_across_sections(self):
        report = make_report()
        self.assertEqual(
            collect_items(report),
            [("finished", "甲"), ("finished", "乙"), ("solved", "丙")],
        )

    def test_format_items_numbers_globally(self):
        text = format_items(make_report())
        self.assertIn("1. 甲", text)
        self.assertIn("3. 丙", text)
        self.assertIn("解决的问题", text)

    def test_format_items_marks_empty_report(self):
        report = {"finished": "", "solved": "", "problems": "", "next_steps": ""}
        self.assertIn("四段都还是空的", format_items(report))

    def test_apply_items_clears_section_that_became_empty(self):
        report = make_report()
        apply_items(report, [("finished", "只剩一条")])
        self.assertEqual(report["finished"], "- 只剩一条")
        self.assertEqual(report["solved"], "")


class HoursTests(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(parse_hours("6.5"), "6.5")

    def test_trailing_unit_is_ignored(self):
        self.assertEqual(parse_hours("7小时"), "7")
        self.assertEqual(parse_hours("7.5h"), "7.5")

    def test_empty_means_not_filled(self):
        self.assertEqual(parse_hours(""), "")

    def test_non_number_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_hours("半天")

    def test_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_hours("30")
        with self.assertRaises(ValueError):
            parse_hours("0")


class EditSessionTests(unittest.TestCase):
    def _run(self, answers, report=None):
        report = report or make_report()
        output = []
        reader = Reader(answers)
        edit_report(report, reader=reader, writer=output.append)
        return report, output

    def test_delete_by_number(self):
        report, _ = self._run(["d", "2", "q"])
        self.assertEqual(report["finished"], "- 甲")

    def test_add_to_chosen_section(self):
        report, _ = self._run(["a", "4", "明天做丁", "q"])
        self.assertEqual(report["next_steps"], "- 明天做丁")

    def test_add_defaults_to_first_section(self):
        report, _ = self._run(["a", "", "默认加这里", "q"])
        self.assertEqual(report["finished"], "- 甲\n- 乙\n- 默认加这里")

    def test_modify_keeps_original_when_enter(self):
        report, _ = self._run(["m", "1", "", "q"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")

    def test_modify_replaces_text(self):
        report, _ = self._run(["m", "1", "甲改", "q"])
        self.assertEqual(report["finished"], "- 甲改\n- 乙")

    def test_add_and_delete_and_modify_together(self):
        report, _ = self._run(["d", "2", "a", "4", "明天做丁", "m", "1", "甲改", "q"])
        self.assertEqual(report["finished"], "- 甲改")
        self.assertEqual(report["solved"], "- 丙")
        self.assertEqual(report["next_steps"], "- 明天做丁")

    def test_hours_can_be_set_and_cleared(self):
        report, _ = self._run(["h", "6.5", "q"])
        self.assertEqual(report["hours"], "6.5")
        report, _ = self._run(["h", "-", "q"], report)
        self.assertEqual(report["hours"], "")

    def test_bad_hours_keeps_previous_value(self):
        report, output = self._run(["h", "30", "q"])
        self.assertEqual(report["hours"], "")
        self.assertTrue(any("0 到 24" in line for line in output))

    def test_unknown_number_does_not_crash(self):
        report, output = self._run(["d", "99", "q"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertTrue(any("没有这个编号" in line for line in output))

    def test_unknown_command_does_not_crash(self):
        report, output = self._run(["x", "q"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertTrue(any("只认" in line for line in output))

    def test_preview_prints_list_without_changing(self):
        report, output = self._run(["p", "q"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertTrue(any("1. 甲" in line for line in output))

    def test_eof_finishes_session(self):
        report, _ = self._run([])
        self.assertEqual(report["finished"], "- 甲\n- 乙")


if __name__ == "__main__":
    unittest.main()
