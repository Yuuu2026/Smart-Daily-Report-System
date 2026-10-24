"""发送前编辑草稿的测试：报编号改一条、添加、删除、改工时。"""

import unittest

from scripts.report_editor import (
    apply_items,
    collect_items,
    edit_one,
    format_items,
    format_numbered,
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

    def test_format_numbered_is_one_line_per_item(self):
        lines = format_numbered(make_report()).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("1. [今日完成] 甲", lines[0])
        self.assertIn("3. [解决的问题] 丙", lines[2])

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


class EditOneTests(unittest.TestCase):
    """e 之后直接就问「改哪一条」「改成什么」，不再先选 m。"""

    def _run(self, answers, report=None):
        report = report or make_report()
        output = []
        reader = Reader(answers)
        changed = edit_one(report, reader=reader, writer=output.append)
        return report, output, changed, reader

    def test_number_then_new_text_edits_directly(self):
        report, output, changed, reader = self._run(["2", "乙改"])
        self.assertEqual(report["finished"], "- 甲\n- 乙改")
        self.assertTrue(changed)
        self.assertTrue(any("改哪一条" in line for line in output))
        self.assertIn("改成", reader.prompts[-1])
        self.assertEqual(len(reader.prompts), 2)

    def test_enter_keeps_original_text(self):
        report, _, changed, _ = self._run(["1", ""])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertFalse(changed)

    def test_unknown_number_is_reported(self):
        report, output, changed, _ = self._run(["99", "随便"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertFalse(changed)
        self.assertTrue(any("没有这个编号" in line for line in output))

    def test_adding_uses_section_number(self):
        report, output, changed, _ = self._run(["a", "4", "明天做丁"])
        self.assertEqual(report["next_steps"], "- 明天做丁")
        self.assertTrue(changed)
        self.assertTrue(any("已添加到" in line for line in output))

    def test_adding_defaults_to_first_section(self):
        report, _, _, _ = self._run(["a", "", "默认加这里"])
        self.assertEqual(report["finished"], "- 甲\n- 乙\n- 默认加这里")

    def test_deleting_by_number(self):
        report, output, changed, _ = self._run(["d", "2"])
        self.assertEqual(report["finished"], "- 甲")
        self.assertTrue(changed)
        self.assertTrue(any("已从「今日完成」删除" in line for line in output))

    def test_hours_can_be_set_and_cleared(self):
        report, _, _, _ = self._run(["h", "6.5"])
        self.assertEqual(report["hours"], "6.5")
        report, _, _, _ = self._run(["h", "-"], report)
        self.assertEqual(report["hours"], "")

    def test_bad_hours_keeps_previous_value(self):
        report, output, changed, _ = self._run(["h", "30"])
        self.assertEqual(report["hours"], "")
        self.assertFalse(changed)
        self.assertTrue(any("0 到 24" in line for line in output))

    def test_preview_lists_items_without_changing(self):
        report, output, changed, _ = self._run(["p"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertFalse(changed)
        self.assertTrue(any("1. 甲" in line for line in output))

    def test_enter_returns_immediately(self):
        report, _, changed, reader = self._run([""])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertFalse(changed)
        self.assertEqual(len(reader.prompts), 1)

    def test_eof_returns_without_changing(self):
        report, _, changed, _ = self._run([])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertFalse(changed)

    def test_non_number_command_is_rejected_with_hint(self):
        report, output, changed, _ = self._run(["x"])
        self.assertEqual(report["finished"], "- 甲\n- 乙")
        self.assertFalse(changed)
        self.assertTrue(any("输入编号" in line for line in output))

    def test_empty_report_offers_add(self):
        report = {"finished": "", "solved": "", "problems": "", "next_steps": ""}
        output = []
        edit_one(report, reader=Reader(["a", "2", "新的一条"]), writer=output.append)
        self.assertEqual(report["solved"], "- 新的一条")
        self.assertTrue(any("还没有任何条目" in line for line in output))


if __name__ == "__main__":
    unittest.main()
