"""本地发布账号：口令哈希与登录校验装饰器。"""

import functools
import hashlib
import hmac
import secrets

from flask import redirect, session, url_for


def hash_password(password):
    """生成 scrypt 口令哈希，格式 scrypt$<salt>$<digest>。"""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password, encoded):
    """校验口令是否与哈希匹配；格式异常一律视为不匹配。"""
    try:
        algorithm, salt_hex, expected_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def login_required(view):
    """未登录时统一跳转登录页，省掉每个路由开头两行样板代码。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper
