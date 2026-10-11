import tempfile
import unittest
from pathlib import Path

from web.app import initialize_database
from web.publisher_app import create_publisher_app


class AuthNavigationLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "daily_report.db"
        initialize_database(database_path)
        self.app = create_publisher_app(database_path, secret_key="test-secret")
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_login_and_register_use_current_app_routes(self):
        for path in ("/login", "/register"):
            with self.subTest(path=path):
                response = self.client.get(path)
                page = response.get_data(as_text=True)

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("http://127.0.0.1:5000", page)
                self.assertIn('href="/archive"', page)


if __name__ == "__main__":
    unittest.main()
