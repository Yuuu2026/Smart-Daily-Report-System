"""交互式发布账号管理：python web/manage_users.py add <用户名> <显示名>"""

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.app import DATABASE_PATH, database_connection, initialize_database
from web.publisher_auth import hash_password


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "add":
        print("用法：python web/manage_users.py add <用户名> <显示名>")
        return 1
    _, username, display_name = args
    if not username.strip() or not display_name.strip():
        print("错误：用户名和显示名不能为空。")
        return 1
    password = getpass.getpass("设置密码（至少 12 个字符）：")
    if len(password) < 12:
        print("错误：密码至少需要 12 个字符。")
        return 1
    confirmation = getpass.getpass("再次输入密码：")
    if password != confirmation:
        print("错误：两次输入的密码不一致。")
        return 1
    try:
        initialize_database(DATABASE_PATH)
        with database_connection(DATABASE_PATH) as connection:
            connection.execute(
                "INSERT INTO publisher_users (username, password_hash, display_name) VALUES (?, ?, ?)",
                (username.strip(), hash_password(password), display_name.strip()),
            )
    except Exception as error:
        # 避免输出包含口令或 SQL 参数的底层异常内容。
        print(f"错误：无法创建账号（{type(error).__name__}）。")
        return 1
    print(f"账号 {username.strip()} 已创建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
