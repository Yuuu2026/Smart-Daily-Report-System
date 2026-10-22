import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / ".claude" / "code_change_summary.py"
if SCRIPT.is_file():
    spec = importlib.util.spec_from_file_location("code_change_summary", SCRIPT)
    code_change_summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(code_change_summary)


@unittest.skipUnless(SCRIPT.is_file(), "本机 .claude hook 脚本未安装")
class ChangeSummaryHookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".claude").mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.state = self.root / ".claude" / "recent_code_changes.json"
        self.file = self.root / "example.py"
        self.file.write_text("print('original')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "example.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "initial"],
            check=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def record(self, path, tool="Edit"):
        return code_change_summary.update_from_event(
            {"tool_name": tool, "tool_input": {"file_path": str(path)}},
            self.root, self.state,
        )

    def test_tracked_change_saves_only_metadata(self):
        self.file.write_text("print('secret-token-value')\nprint('another')\n", encoding="utf-8")

        context = self.record(self.file)
        entries = json.loads(self.state.read_text(encoding="utf-8"))

        self.assertIn("secret-token-value", context)
        self.assertEqual(entries[0]["path"], "example.py")
        self.assertEqual(entries[0]["kind"], "修改")
        self.assertEqual(entries[0]["additions"], 2)
        self.assertEqual(entries[0]["deletions"], 1)
        self.assertNotIn("secret-token-value", self.state.read_text(encoding="utf-8"))

    def test_new_file_and_repeat_edit_keep_most_recent_only(self):
        new_file = self.root / "frontend.css"
        new_file.write_text("a {}\nb {}\n", encoding="utf-8")
        self.record(new_file, "Write")
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))[0]["kind"], "新文件")
        self.file.write_text("print('updated')\n", encoding="utf-8")
        self.record(self.file)
        new_file.write_text("a {}\nb {}\nc {}\n", encoding="utf-8")
        self.record(new_file)
        self.assertEqual(
            [item["path"] for item in json.loads(self.state.read_text(encoding="utf-8"))],
            ["frontend.css", "example.py"],
        )

    def test_ignored_and_outside_files_are_not_recorded(self):
        (self.root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        ignored = self.root / "ignored.py"
        ignored.write_text("print('private')\n", encoding="utf-8")
        internal = self.root / ".claude" / "settings.py"
        internal.write_text("print('internal')\n", encoding="utf-8")
        outside = Path(self.temp_dir.name).parent / "other.py"
        self.assertIsNone(self.record(ignored))
        self.assertIsNone(self.record(internal))
        self.assertIsNone(self.record(outside))
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
