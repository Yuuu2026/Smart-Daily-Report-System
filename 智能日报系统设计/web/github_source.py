"""GitHub REST API 的提交采集适配器。"""

import datetime
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "github_config.json")


class GitHubSourceError(Exception):
    """可直接显示给用户的 GitHub 采集错误。"""


def load_config(config_path=DEFAULT_CONFIG_PATH):
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        raise GitHubSourceError("尚未配置 GitHub 仓库。") from None
    except (OSError, json.JSONDecodeError):
        raise GitHubSourceError("GitHub 配置文件无法读取或格式无效。") from None
    if not isinstance(config, dict):
        raise GitHubSourceError("GitHub 配置文件格式无效。")
    repository = config.get("repository", "")
    if not isinstance(repository, str):
        raise GitHubSourceError("GitHub 仓库配置无效。")
    config["repository"] = normalize_repository(repository)
    members = config.get("members", {})
    if not isinstance(members, dict):
        raise GitHubSourceError("GitHub 成员映射必须是对象。")
    config["members"] = {
        str(login).strip().casefold(): str(name).strip()
        for login, name in members.items()
        if str(login).strip() and str(name).strip()
    }
    return config


def normalize_repository(value):
    value = value.strip()
    if value.startswith(("https://github.com/", "http://github.com/")):
        parsed = urlparse(value)
        value = parsed.path.strip("/")
    value = value.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise GitHubSourceError("GitHub 仓库格式应为 owner/repo。")
    return value


def _retry_delay(error, attempt):
    headers = error.headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(1, min(60, int(retry_after)))
        except ValueError:
            pass
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset_at = int(headers.get("X-RateLimit-Reset", "0"))
            seconds = reset_at - int(time.time())
            if seconds > 60:
                raise GitHubSourceError(
                    f"GitHub API 已达请求上限，请在约 {max(1, seconds // 60)} 分钟后重试。"
                )
            return max(1, seconds)
        except ValueError:
            pass
    return min(60, 2 ** (attempt + 1))


def _request_json(url, token=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "daily-report-collector",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(4):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8")), response.headers.get("Link", "")
        except HTTPError as error:
            headers = error.headers or {}
            is_rate_limited = error.code == 429 or (
                error.code == 403
                and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0")
            )
            if is_rate_limited and attempt < 3:
                delay = _retry_delay(error, attempt)
                time.sleep(delay)
                continue
            if error.code in (401, 403):
                raise GitHubSourceError("GitHub 访问被拒绝，请确认仓库权限或本机访问令牌。") from None
            if error.code == 404:
                raise GitHubSourceError("GitHub 仓库不存在，或当前凭据无权访问。") from None
            raise GitHubSourceError(f"GitHub API 请求失败（HTTP {error.code}）。") from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 3:
                time.sleep(min(8, 2 ** attempt))
                continue
            raise GitHubSourceError("GitHub API 连接失败或响应格式无效，请稍后重试。") from None
    raise GitHubSourceError("GitHub API 重试次数已用完，请稍后重试。")


def _next_link(link_header):
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if match:
            candidate = urlparse(match.group(1))
            if candidate.scheme == "https" and candidate.netloc == "api.github.com":
                return match.group(1)
    return None


def collect_commits(repository, since, until, members=None, token=None):
    """获取时间窗口中的提交和变更统计，返回与存储层解耦的字典列表。"""
    repository = normalize_repository(repository)
    owner, repo = repository.split("/", 1)
    base = f"{API_ROOT}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/commits"
    params = urlencode({"since": since.isoformat(), "until": until.isoformat(), "per_page": 100})
    next_url = f"{base}?{params}"
    summaries = []
    while next_url:
        page, link = _request_json(next_url, token)
        if not isinstance(page, list):
            raise GitHubSourceError("GitHub 返回了无法识别的提交列表。")
        summaries.extend(page)
        next_url = _next_link(link)

    mapped = {str(login).casefold(): name for login, name in (members or {}).items()}
    results = []
    for summary in summaries:
        sha = summary.get("sha")
        if not sha:
            continue
        detail, _ = _request_json(f"{base}/{quote(sha, safe='')}", token)
        commit = detail.get("commit") or {}
        author = detail.get("author") or summary.get("author") or {}
        author_login = author.get("login") or ""
        raw_author = commit.get("author") or {}
        raw_committer = commit.get("committer") or {}
        committed_at = raw_author.get("date") or raw_committer.get("date")
        if not committed_at:
            continue
        try:
            committed = datetime.datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
            committed_utc = committed.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
            work_date = committed.astimezone(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        except ValueError:
            continue
        stats = detail.get("stats") or {}
        results.append({
            "repository": repository,
            "sha": sha,
            "github_login": author_login,
            "member_name": mapped.get(author_login.casefold()) or author_login or (author.get("name") or "未知成员"),
            "work_date": work_date,
            "message": (commit.get("message") or "").strip(),
            "additions": int(stats.get("additions") or 0),
            "deletions": int(stats.get("deletions") or 0),
            "changed_files": len(detail.get("files") or []),
            "commit_url": detail.get("html_url") or summary.get("html_url") or "",
            "committed_at_utc": committed_utc,
        })
    return results


def collect_last_24_hours(config_path=DEFAULT_CONFIG_PATH):
    config = load_config(config_path)
    now = datetime.datetime.now(datetime.timezone.utc)
    token = os.environ.get("DAILY_REPORT_GITHUB_TOKEN", "").strip() or None
    commits = collect_commits(
        config["repository"], now - datetime.timedelta(hours=24), now,
        members=config["members"], token=token,
    )
    return config["repository"], commits
