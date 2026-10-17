import json
import os
import sqlite3

import feishu_source


database_path = os.path.join(os.path.dirname(__file__), "daily_report.db")
connection = sqlite3.connect(database_path)
connection.row_factory = sqlite3.Row
users = connection.execute(
    "SELECT id, display_name, feishu_app_id, feishu_chat_id, feishu_chat_name "
    "FROM publisher_users WHERE feishu_app_id != '' OR feishu_chat_id != ''"
).fetchall()

print("已配置飞书的用户：")
for user in users:
    print(
        json.dumps(
            {
                "id": user["id"],
                "display_name": user["display_name"],
                "app_id": user["feishu_app_id"],
                "chat_id": user["feishu_chat_id"],
                "chat_name": user["feishu_chat_name"],
            },
            ensure_ascii=False,
        )
    )

print("\n凭据检查：")
try:
    configured_app_id = users[0]["feishu_app_id"] if users else ""
    app_id, app_secret = feishu_source.feishu_credentials(configured_app_id)
    print(f"App ID：{app_id}")
    print("App Secret：已保存（不显示明文）")
    import urllib.request

    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print("令牌接口原始响应：", response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print("令牌接口 HTTP", error.code, "原始响应：", error.read().decode("utf-8"))
    token = feishu_source._tenant_access_token(app_id, app_secret)
    print("租户访问令牌：获取成功")
except Exception as error:
    print(f"凭据或令牌失败：{type(error).__name__}: {error}")
    raise SystemExit(0)

print("\n权限探测 1：获取群聊列表 /im/v1/chats（检测 im:chat 权限）")
try:
    chats = feishu_source.list_feishu_chats(app_id, app_secret)
    print(f"成功，可见群聊数：{len(chats)}")
    for chat in chats[:10]:
        print(f"  - {chat['chat_name']}（{chat['chat_id']}）")
except Exception as error:
    print(f"失败：{type(error).__name__}: {error}")

print("\n权限探测 2：读取群消息 /im/v1/messages（检测 im:message 权限）")
chat_id = users[0]["feishu_chat_id"] if users else ""
if not chat_id and chats:
    chat_id = chats[0]["chat_id"]
if chat_id:
    try:
        _, messages = feishu_source.collect_feishu_recent_messages(
            chat_id, app_id=app_id, app_secret=app_secret
        )
        print(f"成功，拉取到 {len(messages)} 条消息。")
    except Exception as error:
        print(f"失败：{type(error).__name__}: {error}")
else:
    print("未找到可用群聊 ID，跳过。")
