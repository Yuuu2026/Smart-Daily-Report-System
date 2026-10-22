"""为智能日报发送任务完成通知邮件。"""

from dataclasses import dataclass
import datetime
import html
import json
import mimetypes
import os
import re
import smtplib
from pathlib import Path
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from report_core import compact_progress_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SMTP_CONFIG_PATH = PROJECT_ROOT / ".smtp.local.json"
LOCAL_SMTP_CREDENTIALS_PATH = PROJECT_ROOT / ".smtp.credentials.json"
LOCAL_SMTP_KEY_PATH = PROJECT_ROOT / ".smtp.key"
KEYRING_SERVICE_NAME = "Smart Daily Report SMTP"
SMTP_PROVIDERS = {
    "qq.com": ("smtp.qq.com", 465, "ssl"),
    "foxmail.com": ("smtp.qq.com", 465, "ssl"),
    "163.com": ("smtp.163.com", 465, "ssl"),
    "126.com": ("smtp.126.com", 465, "ssl"),
    "sina.com": ("smtp.sina.com", 465, "ssl"),
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "outlook.com": ("smtp.office365.com", 587, "starttls"),
    "hotmail.com": ("smtp.office365.com", 587, "starttls"),
    "live.com": ("smtp.office365.com", 587, "starttls"),
}


class EmailNotificationError(Exception):
    """发送邮件失败时使用的中文错误。"""


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    secure: str


def is_valid_email(address):
    """检查邮箱地址是否为常见且无空格的格式。"""
    address = (address or "").strip()
    if not address:
        return False
    name, parsed_address = parseaddr(address)
    if name or parsed_address != address:
        return False
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address) is not None


def _read_local_smtp_config():
    """读取本机 SMTP 配置；该文件不进入 Git 仓库。"""
    if not LOCAL_SMTP_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(LOCAL_SMTP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EmailNotificationError("SMTP 本地配置文件无法读取，请重新运行配置脚本。") from error
    if not isinstance(data, dict):
        raise EmailNotificationError("SMTP 本地配置文件格式不正确，请重新运行配置脚本。")
    return {
        str(key).strip(): ("" if value is None else str(value).strip())
        for key, value in data.items()
        if key in {"host", "port", "secure", "username", "sender"}
    }


def local_sender_email():
    """返回本机配置文件中的默认发件邮箱。"""
    return _read_local_smtp_config().get("sender", "")


def _fernet_cipher():
    """取得本机 Fernet 加密器；密钥文件不进入 Git 仓库。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:
        raise EmailNotificationError(
            "当前 Python 环境缺少 cryptography，无法保存授权码。"
        ) from error

    try:
        if LOCAL_SMTP_KEY_PATH.exists():
            key = LOCAL_SMTP_KEY_PATH.read_text(encoding="ascii").strip()
            return Fernet(key.encode("ascii"))
        key = Fernet.generate_key()
        LOCAL_SMTP_KEY_PATH.write_bytes(key + b"\n")
        return Fernet(key)
    except (OSError, ValueError, TypeError) as error:
        raise EmailNotificationError(
            "SMTP 授权码本地加密密钥保存失败，请检查项目目录权限。"
        ) from error


def _read_local_smtp_credentials():
    """读取本地授权码密文；异常时不回显授权码内容。"""
    if not LOCAL_SMTP_CREDENTIALS_PATH.exists():
        return {}
    try:
        data = json.loads(LOCAL_SMTP_CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EmailNotificationError(
            "SMTP 授权码本地存储文件无法读取，请重新保存授权码。"
        ) from error
    if not isinstance(data, dict):
        raise EmailNotificationError("SMTP 授权码本地存储文件格式不正确。")
    if not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
        raise EmailNotificationError("SMTP 授权码本地存储文件格式不正确。")
    return data


def _write_local_smtp_credentials(username, password):
    """把授权码加密后写入本机私有文件。"""
    try:
        cipher = _fernet_cipher()
        encrypted_password = cipher.encrypt(password.encode("utf-8")).decode("ascii")
        LOCAL_SMTP_CREDENTIALS_PATH.write_text(
            json.dumps(
                {
                    "version": 1,
                    "username": username,
                    "password": encrypted_password,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except EmailNotificationError:
        raise
    except OSError as error:
        raise EmailNotificationError(
            "SMTP 授权码本地加密保存失败，请检查项目目录权限。"
        ) from error


def _smtp_password_from_local_store(username):
    """从本地加密文件读取 SMTP 授权码。"""
    if not username:
        return ""

    data = _read_local_smtp_credentials()
    if data.get("username") != username:
        return ""
    try:
        from cryptography.fernet import InvalidToken

        cipher = _fernet_cipher()
        return cipher.decrypt(data["password"].encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        raise EmailNotificationError(
            "SMTP 授权码本地解密失败，请重新填写并保存授权码。"
        ) from error


def _keyring_password(username):
    """只负责读取 Windows 凭据管理器中的授权码。"""
    if not username:
        return ""
    try:
        import keyring
    except ImportError:
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE_NAME, username) or ""
    except Exception:
        return ""


def _smtp_password_from_keyring(username):
    """优先读 Windows 凭据管理器，登录会话不可用时读本地加密文件。"""
    password = _keyring_password(username)
    if password:
        return password
    return _smtp_password_from_local_store(username)


def smtp_server_for_email(address):
    """根据发件邮箱域名识别常用 SMTP 服务。"""
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    return SMTP_PROVIDERS.get(domain)


def _normalize_smtp_password(password):
    """校验 SMTP 授权码格式，避免非 ASCII 内容进入 SMTP 认证。"""
    password = (password or "").strip()
    if not password.isascii() or any(character.isspace() for character in password):
        raise EmailNotificationError(
            "SMTP 授权码格式不正确：不能包含中文、全角字符或空格。"
            "请重新复制授权码本身，或确认输入的不是邮箱登录密码。"
        )
    return password


def store_smtp_password(username, password):
    """保存 SMTP 授权码；优先系统凭据管理器，失败时降级到本地加密文件。"""
    if not username or not password:
        raise EmailNotificationError("发件邮箱和授权码不能为空。")
    password = _normalize_smtp_password(password)

    keyring_error = None
    try:
        import keyring
    except ImportError as error:
        keyring_error = error
    else:
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, username, password)
            return
        except Exception as error:
            keyring_error = error

    try:
        _write_local_smtp_credentials(username, password)
    except EmailNotificationError as local_error:
        keyring_detail = f"（Windows：{keyring_error}）" if keyring_error else ""
        raise EmailNotificationError(
            "授权码保存失败：Windows 凭据管理器不可用，本地加密文件也保存失败；"
            f"{local_error}{keyring_detail}"
        ) from local_error


def smtp_password_is_saved(username):
    """检查当前发件邮箱是否已有授权码，不回显授权码内容。"""
    return bool(_smtp_password_from_keyring(username))


def write_local_smtp_config(sender_email):
    """根据发件邮箱写入本机 SMTP 服务器配置；授权码不写入文件。"""
    provider = smtp_server_for_email(sender_email)
    if provider is None:
        raise EmailNotificationError("暂未自动识别该邮箱服务商，请使用 QQ、163、126、Gmail 或 Outlook 邮箱。")
    host, port, secure = provider
    try:
        LOCAL_SMTP_CONFIG_PATH.write_text(
            json.dumps(
                {
                    "host": host,
                    "port": port,
                    "secure": secure,
                    "username": sender_email,
                    "sender": sender_email,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise EmailNotificationError("SMTP 本地配置保存失败，请检查项目目录权限。") from error


def smtp_settings_from_env(sender_email=None):
    """读取 SMTP 配置；界面传入的发件邮箱优先于本机配置。"""
    local_config = _read_local_smtp_config()
    sender_email = (sender_email or "").strip()
    host = os.getenv("DAILY_REPORT_SMTP_HOST", "").strip() or local_config.get("host", "")
    if sender_email:
        provider = smtp_server_for_email(sender_email)
        if provider:
            host = os.getenv("DAILY_REPORT_SMTP_HOST", "").strip() or provider[0]
    if not host:
        return None

    provider_port = str(provider[1]) if sender_email and provider else ""
    port_text = (
        os.getenv("DAILY_REPORT_SMTP_PORT", "").strip()
        or provider_port
        or local_config.get("port", "")
        or "587"
    )
    try:
        port = int(port_text)
    except ValueError:
        port = 587

    provider_secure = provider[2] if sender_email and provider else ""
    secure = (
        os.getenv("DAILY_REPORT_SMTP_SECURE", "").strip().lower()
        or provider_secure
        or local_config.get("secure", "").lower()
        or "auto"
    )
    if secure == "auto":
        secure = "ssl" if port == 465 else "starttls"
    if secure not in {"ssl", "starttls", "none"}:
        secure = "starttls"

    username = (
        os.getenv("DAILY_REPORT_SMTP_USERNAME", "").strip()
        or sender_email
        or local_config.get("username", "")
    )
    password = os.getenv("DAILY_REPORT_SMTP_PASSWORD", "").strip()
    if password:
        password = _normalize_smtp_password(password)
    if not password:
        password = _smtp_password_from_keyring(username)
        if password:
            password = _normalize_smtp_password(password)
    if username and not password:
        raise EmailNotificationError("SMTP 授权码未配置，请在工作台的“邮件收发配置”里填写。")

    sender = (
        os.getenv("DAILY_REPORT_EMAIL_FROM", "").strip()
        or sender_email
        or local_config.get("sender", "")
        or username
        or "noreply@example.com"
    )
    return SmtpSettings(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        secure=secure,
    )


def _attach_files(message, attachments):
    """把本地图片文件附加到 EmailMessage，缺失文件会安全跳过。"""
    for attachment in attachments or []:
        path = Path(attachment)
        if not path.is_file():
            continue
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        maintype, _, subtype = content_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )


def _html_escape(value):
    """转义邮件正文中的用户输入，避免破坏 HTML 结构。"""
    return html.escape(str(value or ""), quote=True)


def _html_lines(text):
    """把普通文本转为适合放入 HTML 列表的内容，保留换行。"""
    return _html_escape(text).replace("\n", "<br>")


def _email_section(icon, title, lines, accent="#3d78f5"):
    """渲染一个带图标、圆角和左侧色条的邮件卡片。"""
    items = "".join(f"<li>{_html_lines(line)}</li>" for line in lines)
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="margin:14px 0 0;border-left:4px solid {accent};'
        'border-radius:10px;background:#f7f9ff;">'
        '<tr><td style="padding:15px 18px;">'
        f'<div style="font-size:14px;font-weight:700;color:#26314d;">'
        f'{icon} {_html_escape(title)}</div>'
        f'<ul style="margin:10px 0 0;padding-left:18px;color:#46516a;'
        f'font-size:13px;line-height:1.75;">{items}</ul>'
        "</td></tr></table>"
    )


def _email_wrapper(header_title, header_subtitle, meta_items, sections, footer_note):
    """生成浅色卡片式 HTML 邮件，头像区用蓝色渐变和装饰图标。"""
    meta_chips = ""
    for label, value in meta_items:
        meta_chips += (
            '<div style="display:inline-block;margin:0 8px 8px 0;'
            'padding:6px 11px;border:1px solid #e2e8ff;border-radius:999px;'
            'background:#f7f9ff;color:#6b7690;font-size:12px;">'
            f'<span style="color:#98a2bb;">{_html_escape(label)}</span> '
            f'<strong style="color:#34405a;font-weight:700;">'
            f'{_html_escape(value)}</strong></div>'
        )
    body_sections = "\n".join(sections)
    return f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:0;background-color:#eef3ff;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background-color:#eef3ff;padding:28px 12px;">
    <tr>
      <td align="center">
        <table width="640" cellpadding="0" cellspacing="0" role="presentation"
               style="width:100%;max-width:640px;background-color:#ffffff;
                      border:1px solid #e3e9ff;border-radius:16px;overflow:hidden;">
          <tr>
            <td style="background-color:#3d78f5;
                       background-image:linear-gradient(135deg,rgba(255,255,255,0.16) 25%,transparent 25%),
                       linear-gradient(225deg,rgba(255,255,255,0.12) 25%,transparent 25%);
                       background-size:28px 28px;
                       padding:26px 30px 24px;color:#ffffff;">
              <div style="font-size:12px;font-weight:700;letter-spacing:2px;
                          opacity:0.85;">SMART DAILY REPORT</div>
              <div style="font-size:23px;font-weight:800;margin-top:8px;
                          line-height:1.3;">{_html_escape(header_title)}</div>
              <div style="font-size:13px;margin-top:7px;opacity:0.92;
                          line-height:1.6;">{_html_escape(header_subtitle)}</div>
              <div style="font-size:18px;letter-spacing:6px;margin-top:14px;
                          opacity:0.9;">📌 ☑️ 📬</div>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 26px 26px;">
              <div style="margin-bottom:2px;">{meta_chips}</div>
              {body_sections}
              <div style="margin-top:22px;padding-top:16px;border-top:1px solid #eef1f8;
                          text-align:center;color:#9aa3b8;font-size:11px;line-height:1.7;">
                🤖 智能日报系统自动生成 · {_html_escape(footer_note)}
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def send_test_email(settings, recipient):
    """发送一封 SMTP 配置测试邮件。"""
    if settings is None:
        raise EmailNotificationError("SMTP 邮件服务未配置。")
    if not is_valid_email(recipient):
        raise EmailNotificationError("测试邮箱格式不正确。")

    message = EmailMessage()
    message["Subject"] = "智能日报：SMTP 配置测试成功"
    message["From"] = formataddr(("智能日报", settings.sender))
    message["To"] = recipient.strip()
    generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    sections = [
        _email_section(
            "✅",
            "测试通过",
            ["SMTP 配置已经可用，可以正常发送日报邮件。"],
            accent="#22c55e",
        ),
        _email_section(
            "📬",
            "接下来会发生什么",
            [
                "发布工作进展时，系统会按你填写的收件人发送通知邮件。",
                "同步 GitHub / 飞书仅拉取数据，不会自动发信。",
            ],
            accent="#3d78f5",
        ),
    ]
    html_body = _email_wrapper(
        "SMTP 配置测试成功",
        "智能日报系统的邮件通道已就绪",
        [("收件人", recipient.strip()), ("生成时间", generated_at)],
        sections,
        f"{generated_at} · 本机邮件配置验证通过",
    )
    message.set_content(
        html_body,
        subtype="html",
    )
    _send_message(settings, recipient, message)


def send_progress_email(
    settings,
    recipient,
    display_name,
    work_date,
    finished,
    problems,
    next_steps,
    status="已完成",
    source_url="",
    stream_items=None,
    stream_label=None,
    report_detail="标准",
    attachments=None,
    recent_changes=None,
):
    """发送一条包含工作进展与数据流水的纯文本邮件。"""
    if settings is None:
        raise EmailNotificationError("SMTP 邮件服务未配置。")
    if not is_valid_email(recipient):
        raise EmailNotificationError("通知邮箱格式不正确。")

    stream_items = stream_items or []
    stream_lines = []
    for item in stream_items:
        tag = item.get("tag", "")
        member = item.get("member", "成员")
        summary = item.get("summary", "")
        stream_lines.append(f"- [{tag}] {member}：{summary}")

    message = EmailMessage()
    message["Subject"] = f"研一 {display_name} {work_date} 智能日报"
    message["From"] = formataddr(("智能日报", settings.sender))
    message["To"] = recipient.strip()
    generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    report_detail = report_detail or "标准"
    sections = []
    if report_detail == "简洁":
        concise_lines = [
            part.strip()
            for part in (finished, problems, next_steps)
            if part and part.strip()
        ]
        progress_text = "；".join(concise_lines) or "（未填写）"
        sections.append(
            _email_section("🟦", "今日进展", [progress_text], accent="#38bdf8")
        )
        if stream_lines:
            sections.append(
                _email_section("🔁", stream_label or "数据流水", stream_lines, accent="#8b5cf6")
            )
    elif report_detail == "详细":
        finished_text = finished or "（未填写）"
        problems_text = problems or "（未填写）"
        next_text = next_steps or "（未填写）"
        sections.append(
            _email_section(
                "🧮",
                "系统自动汇总",
                [
                    f"已完成 {1 if finished.strip() else 0} 项主要进展，"
                    f"记录 {1 if problems.strip() else 0} 项问题风险，"
                    f"规划 {1 if next_steps.strip() else 0} 项后续任务。"
                ],
                accent="#38bdf8",
            )
        )
        sections.append(_email_section("✅", "完成事项", [finished_text], accent="#22c55e"))
        sections.append(_email_section("⚠️", "问题与风险", [problems_text], accent="#f59e0b"))
        sections.append(_email_section("🧭", "下一步计划", [next_text], accent="#3d78f5"))
        if stream_lines:
            sections.append(
                _email_section("🔁", stream_label or "数据流水", stream_lines, accent="#8b5cf6")
            )
    else:
        for icon, title, value, accent in (
            ("✅", "完成事项", finished, "#22c55e"),
            ("⚠️", "问题与风险", problems, "#f59e0b"),
            ("🧭", "下一步计划", next_steps, "#3d78f5"),
        ):
            if value:
                sections.append(
                    _email_section(icon, title, [f"- [成员发布] {value}"], accent=accent)
                )
        if stream_lines:
            sections.append(
                _email_section("🔁", stream_label or "数据流水", stream_lines, accent="#8b5cf6")
            )

    if recent_changes:
        change_lines = [
            f"- {item['summary']} · {item['path']}（{item['kind']}，"
            f"相对 HEAD 累计 +{item['additions']} / −{item['deletions']}，"
            f"{item['time'][:16].replace('T', ' ')}）"
            for item in recent_changes
        ]
        sections.append(
            _email_section("🗂️", "最近代码改动", change_lines, accent="#426fa6")
        )

    html_body = _email_wrapper(
        f"研一 {display_name} 智能日报",
        f"{work_date} · {status}",
        [
            ("成员", display_name),
            ("日期", work_date),
            ("状态", status),
        ],
        sections,
        f"{generated_at} · 数据来源：成员发布",
    )
    message.set_content(
        html_body,
        subtype="html",
    )
    _attach_files(message, attachments)

    _send_message(settings, recipient, message)


def send_github_sync_email(
    settings,
    recipient,
    display_name,
    repository,
    status,
    commit_count,
    started_at,
    completed_at,
    commits=None,
    error_message="",
):
    """发送一次 GitHub 仓库同步结果邮件。"""
    commits = commits or []
    succeeded = status == "succeeded"
    status_label = "成功" if succeeded else "失败"

    message = EmailMessage()
    if succeeded:
        message["Subject"] = (
            f"智能日报：GitHub 同步成功（{repository}，{commit_count} 条提交）"
        )
    else:
        message["Subject"] = f"智能日报：GitHub 同步失败（{repository}）"
    message["From"] = formataddr(("智能日报", settings.sender))
    message["To"] = recipient.strip()

    generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    total_additions = sum(int(commit.get("additions", 0) or 0) for commit in commits)
    total_deletions = sum(int(commit.get("deletions", 0) or 0) for commit in commits)

    completed_items = []
    if succeeded:
        completed_items.append(
            f"[系统汇总] 完成 GitHub 同步，归档 {commit_count} 条提交。"
        )
        for commit in commits:
            author = (
                commit.get("member_name")
                or commit.get("github_login")
                or "未知成员"
            )
            commit_message = " ".join(
                str(commit.get("message", "")).splitlines()
            ).strip()
            completed_items.append(
                "[GitHub] "
                + compact_progress_text(
                    f"{author}：{commit_message or '（无提交说明）'}"
                )
            )
        if not commits:
            completed_items.append("[GitHub] 最近 24 小时没有获取到提交记录。")
    else:
        completed_items.append(
            "[GitHub] "
            + compact_progress_text(
                f"同步失败：{error_message or '（未返回错误详情）'}"
            )
        )

    overview_lines = [
        f"- [系统汇总] GitHub 仓库同步{status_label}，归档 {commit_count} 条提交。",
        f"- [系统汇总] 代码变更 +{total_additions} 行 / -{total_deletions} 行。",
    ]
    completion_lines = [f"- {item}" for item in completed_items]
    sections = [
        _email_section(
            "📊",
            "今日概览",
            overview_lines,
            accent="#38bdf8",
        ),
        _email_section(
            "✅" if succeeded else "❌",
            "完成事项" if succeeded else "失败详情",
            completion_lines,
            accent="#22c55e" if succeeded else "#ef4444",
        ),
    ]
    html_body = _email_wrapper(
        "GitHub 同步日报",
        f"{repository} · 同步{status_label}",
        [
            ("发起成员", display_name),
            ("同步结果", status_label),
            ("数据窗口", "最近 24 小时"),
            ("数据来源", f"提交 {commit_count} 条"),
        ],
        sections,
        f"{generated_at} · 代码变更 +{total_additions} / -{total_deletions}",
    )
    message.set_content(
        html_body,
        subtype="html",
    )

    _send_message(settings, recipient, message)


def send_feishu_sync_email(
    settings,
    recipient,
    display_name,
    chat_name,
    status,
    message_count,
    new_count,
    started_at,
    completed_at,
    messages=None,
    error_message="",
):
    """发送一次飞书群聊同步结果邮件。"""
    messages = messages or []
    succeeded = status == "succeeded"
    status_label = "成功" if succeeded else "失败"
    if settings is None:
        raise EmailNotificationError("SMTP 邮件服务未配置。")
    if not is_valid_email(recipient):
        raise EmailNotificationError("通知邮箱格式不正确。")

    message = EmailMessage()
    if succeeded:
        message["Subject"] = (
            f"智能日报：飞书群聊同步成功（{chat_name}，{message_count} 条消息）"
        )
    else:
        message["Subject"] = f"智能日报：飞书群聊同步失败（{chat_name}）"
    message["From"] = formataddr(("智能日报", settings.sender))
    message["To"] = recipient.strip()

    generated_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    completed_items = []
    if succeeded:
        completed_items.append(
            f"[系统汇总] 完成飞书群聊同步，获取 {message_count} 条消息，新增 {new_count} 条协作记录。"
        )
        for item in messages:
            completed_items.append(
                "[飞书] "
                + compact_progress_text(
                    f"{item.get('member_name', '飞书成员')}：{item.get('summary', '')}"
                )
            )
        if not messages:
            completed_items.append("[飞书] 最近 5 分钟没有获取到群聊消息。")
    else:
        completed_items.append(
            "[飞书] " + compact_progress_text(f"同步失败：{error_message or '（未返回错误详情）'}")
        )

    overview_lines = [
        f"- [系统汇总] 飞书群聊「{chat_name}」同步{status_label}。",
        f"- [系统汇总] 获取消息 {message_count} 条，新增协作记录 {new_count} 条。",
    ]
    completion_lines = [f"- {item}" for item in completed_items]
    sections = [
        _email_section(
            "💬",
            "今日概览",
            overview_lines,
            accent="#38bdf8",
        ),
        _email_section(
            "✅" if succeeded else "❌",
            "消息明细" if succeeded else "失败详情",
            completion_lines,
            accent="#22c55e" if succeeded else "#ef4444",
        ),
    ]
    html_body = _email_wrapper(
        "飞书群聊同步日报",
        f"{chat_name} · 同步{status_label}",
        [
            ("发起成员", display_name),
            ("同步结果", status_label),
            ("数据窗口", "最近 5 分钟"),
            ("数据来源", f"消息 {message_count} 条"),
        ],
        sections,
        f"{generated_at} · 新增协作记录 {new_count} 条",
    )
    message.set_content(
        html_body,
        subtype="html",
    )
    _send_message(settings, recipient, message)


def _send_message(settings, recipient, message):
    """校验收件人并提交一封已构建好的邮件。"""
    if settings is None:
        raise EmailNotificationError("SMTP 邮件服务未配置。")
    if not is_valid_email(recipient):
        raise EmailNotificationError("通知邮箱格式不正确。")

    try:
        if settings.secure == "ssl":
            with smtplib.SMTP_SSL(settings.host, settings.port, timeout=12) as smtp:
                if settings.username and settings.password:
                    smtp.login(settings.username, settings.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.host, settings.port, timeout=12) as smtp:
                if settings.secure == "starttls":
                    smtp.starttls()
                if settings.username and settings.password:
                    smtp.login(settings.username, settings.password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError, UnicodeError) as error:
        raise EmailNotificationError(f"邮件发送失败：{error}") from error
