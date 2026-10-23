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

try:  # 兼容两种导入方式：包内相对导入与 web 目录直接运行
    from daily_brief import build_brief
except ImportError:  # pragma: no cover - 仅在特殊启动方式下触发
    from .daily_brief import build_brief


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
        'style="margin:12px 0 0;">'
        '<tr><td style="border:1px solid #e8edf9;border-left:4px solid '
        f'{accent};border-radius:12px;background-color:#fbfcff;padding:15px 18px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>'
        '<td width="26" valign="top" '
        f'style="padding:0;font-size:17px;line-height:24px;">{icon}</td>'
        '<td valign="top" style="padding:0;">'
        '<div style="font-size:14px;font-weight:700;color:#26314d;line-height:1.5;">'
        f"{_html_escape(title)}</div>"
        '<ul style="margin:8px 0 0;padding-left:0;list-style:none;color:#46516a;'
        f'font-size:13px;line-height:1.8;">{items}</ul>'
        "</td></tr></table>"
        "</td></tr></table>"
    )


def _email_block(title, meta, body_html, accent="#3d78f5", top_rule=True):
    """一个带细线分隔的区块标题 + 内容，信息密度比卡片高。

    用两列表格实现左右对齐，邮件客户端对 float 的支持并不可靠。
    """
    rule = "border-top:1px solid #eef1f8;" if top_rule else ""
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="margin:14px 0 0;{rule}">'
        '<tr><td style="padding:13px 0 2px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>'
        '<td valign="middle" style="font-size:13px;font-weight:700;color:#26314d;'
        'line-height:1.6;">'
        f'<span style="color:{accent};">■</span>&nbsp;{_html_escape(title)}</td>'
        '<td valign="middle" align="right" style="font-size:11px;color:#9aa3b8;'
        f'line-height:1.6;">{_html_escape(meta)}</td>'
        "</tr></table>"
        f"{body_html}"
        "</td></tr></table>"
    )


def _email_kpi_grid(cells):
    """概览数字网格；cells 为 [(标签, 数值, 色值)]，按 3 列排。"""
    cells = [cell for cell in cells or [] if cell][:6]
    if not cells:
        return ""
    rows_html = ""
    for start in range(0, len(cells), 3):
        row = cells[start:start + 3]
        tds = ""
        for index, (label, value, tone) in enumerate(row):
            left = "" if index == 0 else "border-left:1px solid #e7ecf8;"
            tds += (
                f'<td width="{round(100 / 3)}%" valign="top" '
                f'style="padding:11px 12px;{left}">'
                '<div style="font-size:11px;color:#8a94ab;line-height:1.5;">'
                f"{_html_escape(label)}</div>"
                '<div style="font-size:20px;font-weight:800;line-height:1.35;'
                f'margin-top:2px;color:{tone};">{_html_escape(value)}</div>'
                "</td>"
            )
        rows_html += f"<tr>{tds}</tr>"
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        'style="margin-top:9px;background-color:#f8faff;border:1px solid #eef2fb;'
        f'border-radius:12px;">{rows_html}</table>'
    )


def _email_tags(tags):
    """标签胶囊行。"""
    tags = [tag for tag in tags or [] if tag]
    if not tags:
        return ""
    chips = "".join(
        '<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 10px;'
        "border-radius:999px;background-color:#eef3ff;color:#3d78f5;font-size:11px;"
        f'line-height:1.7;">{_html_escape(tag)}</span>'
        for tag in tags
    )
    return f'<div style="margin-top:9px;">{chips}</div>'


def _email_checklist(items, marker="✓", tone="#22c55e"):
    """勾选 / 待办清单。"""
    items = [item for item in items or [] if item]
    if not items:
        return ""
    rows = ""
    for item in items:
        rows += (
            "<tr>"
            f'<td width="22" valign="top" style="padding:2px 0;font-size:13px;'
            f'color:{tone};line-height:1.85;">{_html_escape(marker)}</td>'
            '<td valign="top" style="padding:2px 0;font-size:13px;color:#46516a;'
            f'line-height:1.85;">{_html_escape(item)}</td>'
            "</tr>"
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="margin-top:7px;">{rows}</table>'
    )


def _email_bars(rows):
    """占比条形图：外层灰轨道 + 内层按百分比撑开的色块。"""
    rows = [row for row in rows or [] if row.get("label")]
    if not rows:
        return ""
    html_rows = ""
    for row in rows:
        percent = max(2, min(100, int(round(float(row.get("percent") or 0)))))
        if percent >= 50:
            fill = "#3d78f5"
        elif percent >= 25:
            fill = "#7d99f8"
        else:
            fill = "#b9c9f9"
        html_rows += (
            '<tr><td style="padding:5px 0;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>'
            '<td width="92" style="font-size:12px;color:#46516a;line-height:1.7;">'
            f'{_html_escape(row.get("label", ""))}</td>'
            '<td style="padding:0 8px 0 0;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="background-color:#eef1f8;border-radius:4px;">'
            f'<tr><td width="{percent}%" style="height:7px;font-size:0;line-height:0;'
            f'background-color:{fill};border-radius:4px;">&nbsp;</td>'
            f'<td width="{100 - percent}%" style="height:7px;font-size:0;'
            'line-height:0;">&nbsp;</td></tr></table>'
            "</td>"
            '<td width="42" align="right" style="font-size:12px;font-weight:700;'
            f'color:#34405a;">{percent}%</td>'
            "</tr></table></td></tr>"
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="margin-top:8px;">{html_rows}</table>'
    )


def _email_insight(state, advice, tone="steady"):
    """状态 + 建议的一行提示条。"""
    if not state and not advice:
        return ""
    palette = {
        "good": ("#22c55e", "#f3fbf5"),
        "steady": ("#3d78f5", "#f5f8ff"),
        "tight": ("#f59e0b", "#fffaf1"),
        "low": ("#8a94ab", "#f8faff"),
    }
    accent, background = palette.get(tone, palette["steady"])
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        'style="margin-top:8px;background-color:' + background + ";border:1px solid "
        '#eef2fb;border-radius:10px;">'
        '<tr><td width="96" valign="middle" style="padding:10px 0 10px 13px;">'
        '<span style="display:inline-block;padding:2px 9px;border-radius:999px;'
        f'background-color:{accent};color:#ffffff;font-size:11px;font-weight:700;'
        f'line-height:1.7;">{_html_escape(state)}</span></td>'
        '<td valign="middle" style="padding:10px 13px 10px 4px;font-size:12px;'
        f'color:#46516a;line-height:1.75;">{_html_escape(advice)}</td>'
        "</tr></table>"
    )


def _email_stat_cards(cards):
    """一行等宽数据卡片；cards 为 [(标签, 数值, 说明, 色值)]。"""
    cards = [card for card in cards or []][:4]
    if not cards:
        return ""
    width = round(100 / len(cards))
    cells = ""
    for label, value, hint, accent in cards:
        cells += (
            f'<td width="{width}%" valign="top" style="padding:0 4px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border:1px solid #e6ecfa;border-radius:12px;'
            'background-color:#f8faff;">'
            '<tr><td style="padding:13px 14px 12px;">'
            '<div style="font-size:11px;color:#8a94ab;letter-spacing:0.5px;">'
            f"{_html_escape(label)}</div>"
            '<div style="font-size:22px;font-weight:800;line-height:1.35;'
            f'margin-top:4px;color:{accent};">{_html_escape(value)}</div>'
            '<div style="font-size:11px;color:#9aa3b8;margin-top:2px;">'
            f"{_html_escape(hint)}</div>"
            "</td></tr></table></td>"
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="margin:12px 0 4px;"><tr>{cells}</tr></table>'
    )


def _email_bar_chart(title, series, caption=""):
    """用表格实现柱状图：邮件客户端不支持 JavaScript，只能堆 td 高度。"""
    days = (series or {}).get("days") or []
    if not days:
        return ""
    peak = int((series or {}).get("peak") or 0)
    max_bar_height = 74
    cells = ""
    for day in days:
        total = int(day.get("total") or 0)
        if not total:
            height, fill = 6, "#e6ebf7"
        else:
            height = max(12, round(total * max_bar_height / peak)) if peak else 12
            if day.get("is_today"):
                fill = "#3d78f5"
            elif day.get("is_peak"):
                fill = "#7d99f8"
            else:
                fill = "#c2d1fa"
        label_color = "#26314d" if day.get("is_today") else "#5b6a82"
        cells += (
            '<td width="14%" align="center" valign="bottom" style="padding:0 3px;">'
            '<div style="font-size:11px;font-weight:700;line-height:16px;'
            f'color:{label_color};">{total}</div>'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="height:84px;"><tr>'
            '<td valign="bottom" height="84" style="height:84px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
            f'<tr><td height="{height}" bgcolor="{fill}" style="height:{height}px;'
            'border-radius:5px 5px 0 0;font-size:0;line-height:0;">&nbsp;</td></tr>'
            "</table></td></tr></table>"
            '<div style="font-size:11px;line-height:18px;'
            f'color:{label_color};">{_html_escape(day.get("weekday", ""))}</div>'
            "</td>"
        )
    stats = (
        f'近 {len(days)} 天共 {int(series.get("total") or 0)} 条 · '
        f'日均 {series.get("average", 0)} 条'
    )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        'style="margin:16px 0 0;border:1px solid #e6ecfa;border-radius:14px;'
        'background-color:#ffffff;">'
        '<tr><td style="padding:16px 18px 14px;">'
        '<div style="font-size:14px;font-weight:700;color:#26314d;">'
        f"{_html_escape(title)}</div>"
        '<div style="font-size:11px;color:#8a94ab;margin-top:3px;">'
        f"{_html_escape(caption or stats)}</div>"
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="margin-top:12px;"><tr>{cells}</tr></table>'
        '<div style="font-size:11px;color:#a3abbd;margin-top:6px;text-align:right;">'
        f"{_html_escape(stats)}</div>"
        "</td></tr></table>"
    )


def _email_wrapper(
    header_title,
    header_subtitle,
    meta_items,
    sections,
    footer_note,
    summary="",
    stats="",
    chart="",
    badge=None,
):
    """生成浅色卡片式 HTML 邮件：品牌头图 + 数据卡片 + 段落 + 柱状图。"""
    meta_chips = ""
    for label, value in meta_items:
        meta_chips += (
            '<div style="display:inline-block;margin:0 8px 8px 0;'
            'padding:6px 11px;border:1px solid #e2e8ff;border-radius:999px;'
            'background-color:#f7f9ff;color:#6b7690;font-size:12px;">'
            f'<span style="color:#98a2bb;">{_html_escape(label)}</span> '
            '<strong style="color:#34405a;font-weight:700;">'
            f"{_html_escape(value)}</strong></div>"
        )
    badge_html = ""
    if badge:
        badge_top, badge_bottom = badge
        badge_html = (
            '<td width="108" align="right" valign="top" '
            'style="padding:26px 24px 0 0;">'
            '<table cellpadding="0" cellspacing="0" role="presentation" '
            'style="border-radius:14px;background-color:#ffffff;">'
            '<tr><td align="center" style="padding:10px 16px 8px;color:#3d78f5;">'
            '<div style="font-size:11px;font-weight:700;letter-spacing:1px;">'
            f"{_html_escape(badge_top)}</div>"
            '<div style="font-size:26px;font-weight:800;line-height:1.2;">'
            f"{_html_escape(badge_bottom)}</div>"
            "</td></tr></table></td>"
        )
    summary_html = ""
    if summary:
        summary_html = (
            '<div style="margin:4px 0 6px;padding:13px 16px;border-radius:12px;'
            'background-color:#f2f6ff;border:1px solid #e4ebff;color:#33405c;'
            f'font-size:13px;line-height:1.7;">{_html_escape(summary)}</div>'
        )
    body_sections = "\n".join(sections)
    return f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:0;background-color:#eef3ff;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background-color:#eef3ff;padding:28px 12px;">
    <tr>
      <td align="center">
        <table width="660" cellpadding="0" cellspacing="0" role="presentation"
               style="width:100%;max-width:660px;background-color:#ffffff;
                      border:1px solid #e3e9ff;border-radius:18px;overflow:hidden;">
          <tr>
            <td style="background-color:#3d78f5;
                       background-image:linear-gradient(135deg,#4a6bff 0%,#3d78f5 48%,#5b8ef8 100%);
                       padding:0;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
                <tr>
                  <td valign="top" style="padding:26px 10px 24px 28px;color:#ffffff;">
                    <div style="font-size:11px;font-weight:700;letter-spacing:2.4px;
                                color:#dbe4ff;">SMART DAILY REPORT</div>
                    <div style="font-size:23px;font-weight:800;margin-top:9px;
                                line-height:1.3;">{_html_escape(header_title)}</div>
                    <div style="font-size:13px;margin-top:7px;color:#e6ecff;
                                line-height:1.6;">{_html_escape(header_subtitle)}</div>
                  </td>
                  {badge_html}
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 26px 26px;">
              <div style="margin-bottom:2px;">{meta_chips}</div>
              {summary_html}
              {stats}
              {body_sections}
              {chart}
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


def _brief_sections(
    brief,
    status,
    solved,
    finished,
    stream_lines,
    stream_label,
    change_lines,
    detail="标准",
):
    """按「概览 → 总结 → 完成 → 风险 → 改动分布 → 明日计划 → 洞察 → 代码」组装正文。"""
    sections = []
    overview = brief["overview"]

    cells = [
        ("今日任务", f'{overview["tasks"]} 项', "#3d78f5"),
        ("已完成", f'{overview["done"]} 项', "#22c55e"),
        ("进行中", f'{overview["doing"]} 项', "#f59e0b"),
    ]
    if overview["additions"] or overview["deletions"]:
        cells.append(("新增代码", f'+{overview["additions"]}', "#6366f1"))
        cells.append(("删除代码", f'−{overview["deletions"]}', "#8b5cf6"))
    if overview["hours_label"]:
        cells.append(("投入工时", overview["hours_label"], "#0d9488"))
    elif overview["additions"] or overview["deletions"]:
        cells.append(("净增行数", f'{overview["net"]:+d} 行', "#0d9488"))
    overview_meta = status
    if overview["tasks"]:
        overview_meta = f'完成率 {round(overview["done"] * 100 / overview["tasks"])}%'
    sections.append(
        _email_block("今日概览", overview_meta, _email_kpi_grid(cells), top_rule=False)
    )

    summary_body = (
        '<div style="margin-top:8px;font-size:13px;color:#46516a;line-height:1.85;">'
        f'{_html_escape(brief["summary"] or "本次日报未填写正文。")}</div>'
        + _email_tags(brief["tags"])
    )
    sections.append(_email_block("今日总结", "系统自动汇总", summary_body))

    source_labels = []
    if (solved or "").strip():
        source_labels.append("解决的问题")
    if (finished or "").strip():
        source_labels.append("完成事项")
    done_items = list(brief["done_items"])
    if not done_items and detail == "详细":
        done_items = ["（未填写）"]
    if done_items:
        sections.append(
            _email_block(
                "今日完成",
                "来源：" + "、".join(source_labels)
                if source_labels
                else f"{len(done_items)} 项",
                _email_checklist(done_items, "✓", "#22c55e"),
            )
        )

    problem_items = list(brief["problem_items"])
    if not problem_items and detail == "详细":
        problem_items = ["（未填写）"]
    if problem_items:
        sections.append(
            _email_block(
                "问题与风险",
                f"{len(brief['problem_items'])} 项待跟进"
                if brief["problem_items"]
                else "本次未记录",
                _email_checklist(problem_items, "⚠", "#f59e0b"),
            )
        )

    if brief["mix"]:
        sections.append(
            _email_block("改动分布", brief["mix_caption"], _email_bars(brief["mix"]))
        )

    plan_items = list(brief["plan_items"])
    if not plan_items and detail == "详细":
        plan_items = ["（未填写）"]
    if plan_items:
        sections.append(
            _email_block(
                "明日计划",
                f"{len(brief['plan_items'])} 项" if brief["plan_items"] else "待补充",
                _email_checklist(plan_items, "□", "#8a94ab"),
            )
        )

    insight = brief["insight"]
    if insight["state"] and insight["advice"]:
        sections.append(
            _email_block(
                "工作洞察",
                "",
                _email_insight(insight["state"], insight["advice"], insight["tone"]),
            )
        )

    if change_lines:
        totals = brief["change_total"]
        sections.append(
            _email_block(
                "最近代码改动",
                f'+{totals["additions"]} / −{totals["deletions"]}',
                _email_checklist(change_lines, "·", "#426fa6"),
            )
        )

    if stream_lines:
        sections.append(
            _email_block(
                stream_label or "数据流水",
                f"{len(stream_lines)} 条",
                _email_checklist(stream_lines, "·", "#8b5cf6"),
            )
        )
    return sections


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
        summary="这是一封测试邮件：收到它说明发件邮箱、授权码与收件地址都已配置正确。",
        stats=_email_stat_cards(
            [
                ("发件通道", "已连通", settings.host, "#22c55e"),
                ("加密方式", settings.secure.upper(), f"端口 {settings.port}", "#3d78f5"),
                ("收件人", "1 位", "本次测试", "#6366f1"),
            ]
        ),
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
    recent_changes_text=None,
    solved="",
    workload=None,
    hours=None,
):
    """发送一条包含工作进展与数据流水的 HTML 邮件。"""
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
    brief = build_brief(
        solved=solved,
        finished=finished,
        problems=problems,
        next_steps=next_steps,
        recent_changes=recent_changes,
        workload=workload,
        hours=hours,
    )
    change_lines = []
    if recent_changes_text:
        change_lines = [
            line.strip().lstrip("-").strip()
            for line in recent_changes_text.splitlines()
            if line.strip()
        ]
    elif recent_changes:
        change_lines = [
            f"{item['summary']} · {item['path']}（{item['kind']}，"
            f"+{item['additions']} / −{item['deletions']}，"
            f"{item['time'][:16].replace('T', ' ')}）"
            for item in recent_changes
        ]

    if report_detail == "简洁":
        concise_lines = [
            f"{label}：{part.strip()}"
            for label, part in (
                ("解决问题", solved),
                ("完成事项", finished),
                ("问题与风险", problems),
                ("下一步", next_steps),
            )
            if part and part.strip()
        ]
        sections = [
            _email_block(
                "今日进展",
                work_date,
                _email_checklist(
                    ["；".join(concise_lines) or "（未填写）"], "·", "#38bdf8"
                ),
                top_rule=False,
            )
        ]
        if change_lines:
            totals = brief["change_total"]
            sections.append(
                _email_block(
                    "最近代码改动",
                    f'+{totals["additions"]} / −{totals["deletions"]}',
                    _email_checklist(change_lines, "·", "#426fa6"),
                )
            )
        if stream_lines:
            sections.append(
                _email_block(
                    stream_label or "数据流水",
                    f"{len(stream_lines)} 条",
                    _email_checklist(stream_lines, "·", "#8b5cf6"),
                )
            )
    else:
        sections = _brief_sections(
            brief,
            status,
            solved,
            finished,
            stream_lines,
            stream_label,
            change_lines,
            detail=report_detail,
        )

    badge = None
    date_parts = str(work_date).split("-")
    if len(date_parts) == 3:
        badge = (f"{int(date_parts[1])}月", str(int(date_parts[2])))

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
        summary="",
        stats="",
        chart=_email_bar_chart("近 7 天工作量", workload, "按天统计归档的进展条目数"),
        badge=badge,
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
