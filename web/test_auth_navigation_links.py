import tempfile
import unittest
from pathlib import Path

from web.app import database_connection, initialize_database
from web.publisher_app import create_publisher_app
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

    def test_archive_is_the_first_nav_item(self):
        self._login(self.client)

        page = self.client.get("/archive").get_data(as_text=True)

        self.assertLess(page.index("日报归档"), page.index("工作台"))

    def test_navigation_exposes_github_activity(self):
        self._login(self.client)
        with self.client.session_transaction() as flask_session:
            flask_session["selected_source"] = "github"

        for path in ("/archive", "/workbench", "/github"):
            with self.subTest(path=path):
                page = self.client.get(path).get_data(as_text=True)
                self.assertIn('href="/github"', page)


if __name__ == "__main__":
    unittest.main()
