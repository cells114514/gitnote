"""Small GitHub REST client with independent Search budget and response headers."""

from collections import deque
from datetime import datetime, timezone
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cache import PublicCache


API_BASE = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, message, status=0, retry_at=None):
        super().__init__(message)
        self.status = status
        self.retry_at = retry_at


def default_transport(url, headers):
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=12) as response:
            return response.status, response.read(2_000_000), dict(response.headers)
    except HTTPError as error:
        return error.code, error.read(100_000), dict(error.headers)
    except (URLError, TimeoutError) as error:
        raise GitHubError("连接 GitHub 超时，请稍后重试") from error


class GitHubClient:
    def __init__(self, cache=None, transport=None):
        self.cache = cache if cache is not None else PublicCache()
        self.transport = transport or default_transport
        self.lock = threading.Lock()
        self.search_times = {"anonymous": deque(), "authenticated": deque()}
        self.backoff_until = 0
        self.bucket_backoff_until = {}
        self.rate = {}
        self.rate_by_group = {}

    def _budget(self, authenticated):
        group = "authenticated" if authenticated else "anonymous"
        limit = 20 if authenticated else 6
        now = time.time()
        with self.lock:
            retry_at = max(self.backoff_until, self.bucket_backoff_until.get(("search", group), 0))
            if now < retry_at:
                raise GitHubError("GitHub 搜索暂时受限", 429, retry_at)
            values = self.search_times[group]
            while values and values[0] <= now - 60:
                values.popleft()
            if len(values) >= limit:
                raise GitHubError("本地搜索预算已用完", 429, values[0] + 60)
            values.append(now)

    def _record_headers(self, headers, authenticated):
        h = {str(k).lower(): v for k, v in headers.items()}
        resource = h.get("x-ratelimit-resource") or "core"
        with self.lock:
            snapshot = {
                "limit": _int(h.get("x-ratelimit-limit")),
                "remaining": _int(h.get("x-ratelimit-remaining")),
                "reset": _int(h.get("x-ratelimit-reset")),
                "resource": resource,
            }
            self.rate[resource] = snapshot
            group = "authenticated" if authenticated else "anonymous"
            self.rate_by_group[(resource, group)] = snapshot
        return h

    def get_json(self, path, params=None, token=None, search=False, cache_ttl=0,
                 accept="application/vnd.github+json", as_text=False, max_bytes=0):
        query = urlencode(params or {})
        url = API_BASE + path + ("?" + query if query else "")
        cache_key = f"github:{accept}:" + url if cache_ttl and not token else None
        if cache_key:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        if search:
            self._budget(bool(token))
        else:
            with self.lock:
                group = "authenticated" if token else "anonymous"
                retry_at = max(self.backoff_until, self.bucket_backoff_until.get(("core", group), 0))
            if time.time() < retry_at:
                raise GitHubError("GitHub 请求暂时受限", 429, retry_at)
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "gitnote-local"}
        if token:
            headers["Authorization"] = "Bearer " + token
        status, body, response_headers = self.transport(url, headers)
        h = self._record_headers(response_headers, bool(token))
        if status in (403, 429):
            retry = _int(h.get("retry-after"))
            remaining = _int(h.get("x-ratelimit-remaining"))
            reset = _int(h.get("x-ratelimit-reset"))
            now = time.time()
            wait_until = now + retry if retry is not None else (reset if remaining == 0 and reset else now + 60)
            with self.lock:
                if remaining == 0:
                    group = "authenticated" if token else "anonymous"
                    bucket = (h.get("x-ratelimit-resource") or ("search" if search else "core"), group)
                    self.bucket_backoff_until[bucket] = max(self.bucket_backoff_until.get(bucket, 0), wait_until)
                else:
                    self.backoff_until = max(self.backoff_until, wait_until)
            raise GitHubError("GitHub 请求受限，请稍后重试", status, wait_until)
        if status == 401:
            raise GitHubError("GitHub 登录已失效，请重新连接", status)
        if status == 404:
            raise GitHubError("仓库或 README 不存在", status)
        if status == 422:
            raise GitHubError("GitHub 不接受此搜索条件", status)
        if status >= 500:
            raise GitHubError("GitHub 服务暂时不可用", status)
        if status < 200 or status >= 300:
            raise GitHubError("GitHub 请求失败", status)
        if max_bytes and len(body) > max_bytes:
            raise GitHubError("README 内容过大，请在 GitHub 阅读原文", 413)
        if as_text:
            data = body.decode("utf-8", "replace")
            if not data.lstrip().startswith("<"):
                raise GitHubError("GitHub 返回了无法读取的 README", 502)
        else:
            try:
                data = json.loads(body)
            except (ValueError, TypeError) as error:
                raise GitHubError("GitHub 返回了无法读取的数据", status) from error
        if cache_key:
            self.cache.put(cache_key, data, cache_ttl)
        return data

    def get_html(self, path, token=None, cache_ttl=21600):
        return self.get_json(path, token=token, cache_ttl=cache_ttl,
                             accept="application/vnd.github.html+json", as_text=True,
                             max_bytes=750_000)

    def search(self, params, token=None, cache_ttl=6 * 3600):
        # Search data is public, including when fetched using a session token.
        query = urlencode(params)
        key = f"search:{cache_ttl}:" + query
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        data = self.get_json("/search/repositories", params, token, search=True)
        self.cache.put(key, data, cache_ttl)
        return data

    def rate_snapshot(self, authenticated=None):
        with self.lock:
            if authenticated is None:
                resources = dict(self.rate)
                waits = self.bucket_backoff_until.values()
            else:
                group = "authenticated" if authenticated else "anonymous"
                resources = {resource: value for (resource, kind), value in self.rate_by_group.items()
                             if kind == group}
                waits = [wait for (_, kind), wait in self.bucket_backoff_until.items() if kind == group]
            retry_at = max([self.backoff_until, *waits])
            return {"resources": resources, "retry_at": retry_at or None}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
