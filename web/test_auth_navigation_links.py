import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.app import database_connection, initialize_database
from web.publisher_app import create_publisher_app, recent_code_changes
from web.publisher_auth import hash_password


class AuthNavigationLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "daily_report.db"
        initialize_database(self.database_path)
        with database_connection(self.database_path) as connection:
            connection.execute(
                """INSERT INTO publisher_users
                   (username, password_hash, display_name)
                   VALUES (?, ?, ?)""",
                ("zhangsan", hash_password("safe-password-123"), "张三"),
            )
        self.app = create_publisher_app(self.database_path, secret_key="test-secret")
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _login(self, client):
        with client.session_transaction() as flask_session:
            flask_session["csrf_token"] = "login-csrf"
        return client.post(
            "/login",
            data={
                "csrf_token": "login-csrf",
                "username": "zhangsan",
                "password": "safe-password-123",
            },
            follow_redirects=False,
        )

    def test_login_and_register_use_current_app_routes(self):
        for path in ("/login", "/register"):
            with self.subTest(path=path):
                response = self.client.get(path)
                page = response.get_data(as_text=True)

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("http://127.0.0.1:5000", page)
                self.assertIn('href="/archive"', page)

    def test_root_requires_login_then_leads_to_archive(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

        self.assertEqual(self._login(self.client).headers["Location"], "/archive")

        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/archive")

    def test_workbench_keeps_its_own_address(self):
        self._login(self.client)
        with self.client.session_transaction() as flask_session:
            flask_session["selected_source"] = "github"

        response = self.client.get("/workbench")

        self.assertEqual(response.status_code, 200)
        self.assertIn("工作台", response.get_data(as_text=True))

    def test_navigation_has_only_archive_and_sources(self):
        self._login(self.client)

        page = self.client.get("/archive").get_data(as_text=True)
        nav_start = page.index('class="dashboard-nav"')
        nav = page[nav_start:page.index("</nav>", nav_start)]

        self.assertIn('href="/archive"', nav)
        self.assertIn('href="/sources"', nav)
        self.assertNotIn("工作台", nav)
        self.assertNotIn("GitHub 活动", nav)
        self.assertLess(nav.index("日报归档"), nav.index("接入配置"))

    def test_sources_page_offers_both_modules_and_keeps_nav(self):
        self._login(self.client)

        page = self.client.get("/sources").get_data(as_text=True)

        self.assertIn("接入配置", page)
        self.assertIn('href="/sources"', page)
        self.assertIn('href="/workbench?source=github"', page)
        self.assertIn('href="/workbench?source=feishu"', page)
        self.assertIn("连接 GitHub", page)
        self.assertIn("连接飞书", page)
        self.assertIn("待连接", page)
        self.assertIn("primary-pages.css", page)

    def test_archive_keeps_filters_and_empty_state(self):
        self._login(self.client)

        page = self.client.get("/archive?from=2026-09-01&to=2026-09-30&q=not-found").get_data(as_text=True)

        self.assertIn('class="dashboard-page primary-page"', page)
        self.assertIn("primary-pages.css", page)
        self.assertIn('name="from" value="2026-09-01"', page)
        self.assertIn('name="to" value="2026-09-30"', page)
        self.assertIn('name="q" value="not-found"', page)
        self.assertIn("这个时间范围还没有归档记录", page)

    def test_workbench_does_not_load_primary_page_styles(self):
        self._login(self.client)
        with self.client.session_transaction() as flask_session:
            flask_session["selected_source"] = "github"

        page = self.client.get("/workbench").get_data(as_text=True)

        self.assertNotIn("primary-pages.css", page)
        self.assertNotIn('class="dashboard-page primary-page"', page)

    def test_recent_changes_are_login_only_and_escaped(self):
        response = self.client.get("/archive")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")
        self._login(self.client)

        changes = [{
            "path": "web/<script>alert(1)</script>.py",
            "summary": "调整日报归档页面",
            "kind": "修改",
            "time": "2026-10-22T12:34+08:00",
            "additions": 3,
            "deletions": 1,
        }]
        with patch("web.publisher_app.recent_code_changes", return_value=changes):
            page = self.client.get("/archive").get_data(as_text=True)
        self.assertIn("最近代码改动", page)
        self.assertIn("调整日报归档页面", page)
        self.assertIn("web/&lt;script&gt;alert(1)&lt;/script&gt;.py", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_recent_changes_missing_or_bad_json_falls_back_to_empty(self):
        state = Path(self.temp_dir.name) / "recent_changes.json"
        self.assertEqual(recent_code_changes(state), [])
        state.write_text("not-json", encoding="utf-8")
        self.assertEqual(recent_code_changes(state), [])
        self._login(self.client)
        with patch("web.publisher_app.recent_code_changes", return_value=[]):
            page = self.client.get("/archive").get_data(as_text=True)
        self.assertIn("暂无由 Claude Code 记录的代码改动", page)

    def test_recent_changes_loader_limits_entries_and_length(self):
        state = Path(self.temp_dir.name) / "recent_changes.json"
        entries = [{
            "path": "x" * 300,
            "summary": "摘要",
            "kind": "修改",
            "time": "2026-10-22T12:34+08:00",
            "additions": 2,
            "deletions": 1,
        }] * 7
        state.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        loaded = recent_code_changes(state)
        self.assertEqual(len(loaded), 5)
        self.assertEqual(len(loaded[0]["path"]), 160)


if __name__ == "__main__":
    unittest.main()
