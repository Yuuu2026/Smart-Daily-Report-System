"""飞书开放平台群聊消息采集适配器。"""

import datetime
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://open.feishu.cn/open-apis"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_FEISHU_CREDENTIALS_PATH = PROJECT_ROOT / ".feishu.credentials.json"
LOCAL_FEISHU_KEY_PATH = PROJECT_ROOT / ".feishu.key"
KEYRING_SERVICE_NAME = "Smart Daily Report Feishu"
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))


class FeishuSourceError(Exception):
    """可直接展示给用户的飞书接入错误。"""


def _normalize_secret(value):
    value = (value or "").strip()
    if not value:
        raise FeishuSourceError("飞书 App Secret 不能为空。")
    if any(character.isspace() for character in value):
        raise FeishuSourceError("飞书 App Secret 不能包含空格，请重新复制。")
    return value


def _fernet_cipher():
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:
        raise FeishuSourceError(
            "当前 Python 环境缺少 cryptography，无法保存飞书 App Secret。"
        ) from error

    try:
        if LOCAL_FEISHU_KEY_PATH.exists():
            key = LOCAL_FEISHU_KEY_PATH.read_text(encoding="ascii").strip()
            return Fernet(key.encode("ascii"))
        key = Fernet.generate_key()
        LOCAL_FEISHU_KEY_PATH.write_bytes(key + b"\n")
        return Fernet(key)
    except (OSError, ValueError, TypeError) as error:
        raise FeishuSourceError("飞书本地加密密钥保存失败，请检查项目目录权限。") from error


def _write_local_credentials(app_id, app_secret):
    try:
        cipher = _fernet_cipher()
        encrypted_secret = cipher.encrypt(app_secret.encode("utf-8")).decode("ascii")
        LOCAL_FEISHU_CREDENTIALS_PATH.write_text(
            json.dumps(
                {
                    "version": 1,
                    "app_id": app_id,
                    "app_secret": encrypted_secret,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise FeishuSourceError("飞书 App Secret 本地加密保存失败，请检查项目目录权限。") from error


def _local_app_secret(app_id):
    if not LOCAL_FEISHU_CREDENTIALS_PATH.exists():
        return ""
    try:
        data = json.loads(LOCAL_FEISHU_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if data.get("app_id") != app_id:
            return ""
        from cryptography.fernet import InvalidToken

        cipher = _fernet_cipher()
        return cipher.decrypt(data["app_secret"].encode("ascii")).decode("utf-8")
    except (OSError, json.JSONDecodeError, KeyError, InvalidToken, ValueError):
        raise FeishuSourceError("飞书 App Secret 本地解密失败，请重新保存。") from None


def store_feishu_app_secret(app_id, app_secret):
    """保存飞书 App Secret；优先系统凭据管理器，失败时使用本地加密文件。"""
    app_id = (app_id or "").strip()
    app_secret = _normalize_secret(app_secret)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", app_id):
        raise FeishuSourceError("飞书 App ID 格式不正确。")

    try:
        import keyring
    except ImportError:
        keyring = None

    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, app_id, app_secret)
            return
        except Exception:
            pass
    _write_local_credentials(app_id, app_secret)


def _saved_app_secret(app_id):
    if not app_id:
        return ""
    try:
        import keyring
    except ImportError:
        keyring = None

    if keyring is not None:
        try:
            secret = keyring.get_password(KEYRING_SERVICE_NAME, app_id)
            if secret:
                return _normalize_secret(secret)
        except Exception:
            pass
    local_secret = _local_app_secret(app_id)
    return _normalize_secret(local_secret) if local_secret else ""


def feishu_credentials(app_id=None):
    """读取飞书凭据；环境变量优先，其次系统凭据管理器和本地加密文件。"""
    app_id = (app_id or os.environ.get("DAILY_REPORT_FEISHU_APP_ID", "")).strip()
    app_secret = os.environ.get("DAILY_REPORT_FEISHU_APP_SECRET", "").strip()
    if not app_secret:
        app_secret = _saved_app_secret(app_id)
    if not app_id or not app_secret:
        raise FeishuSourceError(
            "飞书接口未配置，请在“飞书群聊”页面填写 App ID 和 App Secret。"
        )
    return app_id, app_secret


def _request_json(path, payload=None, token=None, query=None):
    url = f"{API_ROOT}{path}"
    if query:
        url += "?" + urlencode(query)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("msg", "")
        except Exception:
            pass
        if error.code == 401:
            raise FeishuSourceError("飞书访问令牌无效，请确认 App ID 和 App Secret。") from None
        if error.code == 404 and path.startswith("/im/v1/"):
            hint = (
                "未找到该群聊或机器人尚未加入该群，请确认机器人已加入群聊"
                "并开通 im:chat:readonly / im:message:readonly 权限。"
            )
            if detail:
                hint += f"（接口返回：{detail}）"
            raise FeishuSourceError(hint) from None
        message = f"飞书 API 请求失败（HTTP {error.code}，接口：{path}）"
        if detail:
            message += f"：{detail}"
        raise FeishuSourceError(message) from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise FeishuSourceError("飞书 API 连接失败或响应格式无效，请稍后重试。") from None

    if not isinstance(result, dict) or result.get("code") not in (0, None):
        message = result.get("msg", "未知错误") if isinstance(result, dict) else "响应格式无效"
        raise FeishuSourceError(f"飞书 API 返回错误：{message}")
    return result


def _tenant_access_token(app_id, app_secret):
    result = _request_json(
        "/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    token = result.get("tenant_access_token", "")
    if not token:
        raise FeishuSourceError("飞书未返回访问令牌，请确认应用凭据。")
    return token


def list_feishu_chats(app_id=None, app_secret=None):
    """返回当前飞书应用可见的群聊列表。"""
    app_id, app_secret = _resolve_credentials(app_id, app_secret)
    token = _tenant_access_token(app_id, app_secret)
    chats = []
    page_token = ""
    while True:
        query = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        result = _request_json("/im/v1/chats", token=token, query=query)
        data = result.get("data") or {}
        for item in data.get("items") or []:
            chat_id = item.get("chat_id", "")
            chat_name = (item.get("name") or "未命名群聊").strip()
            if chat_id:
                chats.append(
                    {
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "description": (item.get("description") or "").strip(),
                    }
                )
        page_token = data.get("page_token", "")
        if not data.get("has_more") or not page_token:
            break
    return sorted(chats, key=lambda chat: chat["chat_name"])


def _chat_members(chat_id, token):
    members = {}
    page_token = ""
    while True:
        query = {"member_id_type": "open_id", "page_size": 100}
        if page_token:
            query["page_token"] = page_token
        result = _request_json(
            f"/im/v1/chats/{chat_id}/members",
            token=token,
            query=query,
        )
        data = result.get("data") or {}
        for item in data.get("items") or []:
            member_id = item.get("member_id", "")
            member_name = (item.get("name") or "").strip()
            if member_id and member_name:
                members[member_id] = member_name
        page_token = data.get("page_token", "")
        if not data.get("has_more") or not page_token:
            break
    return members


def _message_text(item):
    body = item.get("body") or {}
    content = (body.get("content") or item.get("content") or "").strip()
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content or f"[消息类型：{item.get('msg_type', '未知')}]"

    if not isinstance(parsed, dict):
        return content
    msg_type = item.get("msg_type", "")
    if msg_type == "text" and parsed.get("text"):
        return str(parsed["text"]).strip()
    if msg_type == "post":
        parts = []
        if parsed.get("title"):
            parts.append(str(parsed["title"]).strip())

        def walk(value):
            if isinstance(value, dict):
                if value.get("tag") == "text" and value.get("text"):
                    parts.append(str(value["text"]).strip())
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(parsed.get("content"))
        if parts:
            return " ".join(part for part in parts if part)
    if parsed.get("text"):
        return str(parsed["text"]).strip()
    if parsed.get("title"):
        return str(parsed["title"]).strip()
    return f"[消息类型：{msg_type or '未知'}]"


def _collect_messages(chat_id, token, start_time, end_time):
    messages = []
    page_token = ""
    while True:
        query = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(int(start_time.timestamp())),
            "end_time": str(int(end_time.timestamp())),
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if page_token:
            query["page_token"] = page_token
        result = _request_json(
            "/im/v1/messages",
            token=token,
            query=query,
        )
        data = result.get("data") or {}
        for item in data.get("items") or []:
            messages.append(item)
        page_token = data.get("page_token", "")
        if not data.get("has_more") or not page_token:
            break
    return messages


def _resolve_credentials(app_id, app_secret):
    if app_id and app_secret:
        return app_id.strip(), _normalize_secret(app_secret)
    configured_app_id, configured_secret = feishu_credentials(app_id)
    return configured_app_id, configured_secret


def collect_feishu_recent_messages(chat_id, app_id=None, app_secret=None):
    """获取指定群聊最近 5 分钟的消息，并归一为日报协作记录。"""
    chat_id = (chat_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", chat_id):
        raise FeishuSourceError("请先选择有效的飞书群聊。")

    app_id, app_secret = _resolve_credentials(app_id, app_secret)
    token = _tenant_access_token(app_id, app_secret)
    chats = list_feishu_chats(app_id, app_secret)
    selected = next((chat for chat in chats if chat["chat_id"] == chat_id), None)
    if selected is None:
        raise FeishuSourceError("未找到所选群聊，请确认应用已被拉入该群。")

    try:
        members = _chat_members(chat_id, token)
    except FeishuSourceError:
        members = {}

    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(minutes=5)
    raw_messages = _collect_messages(chat_id, token, start, now)
    chat_name = selected["chat_name"]
    collected_at = now.isoformat(timespec="seconds")
    results = []
    for item in raw_messages:
        if item.get("deleted") or item.get("updated"):
            continue
        if item.get("msg_type") == "system":
            continue
        message_id = item.get("message_id", "")
        if not message_id:
            continue
        create_time = str(item.get("create_time") or "")
        try:
            created_at = datetime.datetime.fromtimestamp(
                int(create_time) / 1000,
                tz=datetime.timezone.utc,
            )
        except (ValueError, OverflowError, OSError):
            continue
        sender = item.get("sender") or {}
        sender_id = sender.get("id") or item.get("sender_id") or ""
        member_name = members.get(sender_id) or "飞书成员"
        summary = _message_text(item).strip()
        if not summary:
            continue
        results.append(
            {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "sender_id": sender_id,
                "member_name": member_name,
                "msg_type": item.get("msg_type", ""),
                "work_date": created_at.astimezone(BEIJING_TZ).date().isoformat(),
                "summary": summary[:1000],
                "created_at_utc": created_at.isoformat(timespec="seconds"),
                "collected_at_utc": collected_at,
            }
        )
    return chat_name, results


def feishu_sync_page_path():
    """返回飞书页面路由，便于调用方保持链接一致。"""
    return "/feishu"
