"""配置智能日报的 SMTP 服务，并复用 Web 界面的授权码保存策略。"""

from getpass import getpass
import json
from pathlib import Path

from web.email_notifications import (
    LOCAL_SMTP_CONFIG_PATH,
    EmailNotificationError,
    send_test_email,
    smtp_settings_from_env,
    smtp_password_is_saved,
    store_smtp_password,
)


def prompt(label, default=""):
    suffix = f"（默认：{default}）" if default else ""
    value = input(f"{label}{suffix}：").strip()
    return value or default


def main():
    print("智能日报 SMTP 配置向导")
    print("QQ 邮箱需要在网页版“设置 -> 账号”中开启 SMTP 服务并生成授权码。")
    print("授权码不是 QQ 密码；优先保存到 Windows 凭据管理器，不可用时使用本地加密存储。")
    print()

    host = prompt("SMTP 服务器", "smtp.qq.com")
    port_text = prompt("SMTP 端口", "465")
    try:
        port = int(port_text)
    except ValueError:
        print("SMTP 端口必须是数字。")
        return 1
    if not 1 <= port <= 65535:
        print("SMTP 端口必须在 1-65535 之间。")
        return 1

    secure = prompt("加密方式（ssl / starttls / none）", "ssl" if port == 465 else "starttls").lower()
    if secure not in {"ssl", "starttls", "none"}:
        print("加密方式只能是 ssl、starttls 或 none。")
        return 1

    username = prompt("发件邮箱（SMTP 用户名）")
    sender = prompt("邮件显示发件地址", username)
    if not username:
        print("发件邮箱不能为空。")
        return 1

    existing_password = smtp_password_is_saved(username)
    if existing_password:
        password = getpass("输入新的 SMTP 授权码（留空保留已保存授权码）：")
    else:
        password = getpass("输入 SMTP 授权码（输入时不会显示）：")
    if not password and not existing_password:
        print("授权码不能为空。")
        return 1

    try:
        if password:
            store_smtp_password(username, password)
        LOCAL_SMTP_CONFIG_PATH.write_text(
            json.dumps(
                {
                    "host": host,
                    "port": port,
                    "secure": secure,
                    "username": username,
                    "sender": sender,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except (EmailNotificationError, OSError) as error:
        print(f"保存 SMTP 配置失败：{error}")
        return 1

    print()
    print(f"SMTP 配置已保存到：{LOCAL_SMTP_CONFIG_PATH}")
    print("授权码已保存；优先使用 Windows 凭据管理器，系统不可用时使用本地加密存储。")

    answer = input("是否立即发送一封测试邮件？(y/N)：").strip().lower()
    if answer in {"y", "yes"}:
        recipient = prompt("测试收件邮箱", username)
        try:
            send_test_email(smtp_settings_from_env(), recipient)
        except EmailNotificationError as error:
            print(f"测试邮件发送失败：{error}")
            return 1
        print(f"测试邮件已发送到：{recipient}")

    print()
    print("配置完成。请重新启动网站后再发布工作进展或同步 GitHub。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
