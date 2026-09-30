"""Local GitHub waterfall discovery. Run with: python app.py"""

import argparse
import html
import json
import re
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from discovery import TOPICS, STAR_BANDS, discover, search_keyword, stars_top
from github_auth import AuthManager, COOKIE_NAME
from github_client import GitHubClient, GitHubError
from taste_profile import apply_session_decay, apply_signal, clean_profile, observe_repos
from taste_ranking import rank_batch
from trending import trending_page


WEB_DIR = Path(__file__).resolve().parent / "web"
CLIENT = GitHubClient()
AUTH = AuthManager(CLIENT)
REPO_NAME = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\Z")


def clean_text(value, max_length=100):
    return value.strip()[:max_length] if isinstance(value, str) else ""


def clean_search_filters(data):
    language = clean_text(data.get("language"), 40)
    topic = clean_text(data.get("topic"), 80)
    band = clean_text(data.get("star_band"), 40)
    if ((language and not re.fullmatch(r"[A-Za-z0-9+#-]{1,40}", language))
            or (topic and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,79}", topic))
            or (band and band not in {choice for choice, _ in STAR_BANDS})):
        raise ValueError("筛选条件无效")
    return language, topic, band


def clean_page(value):
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise ValueError("页码无效") from None
    if not 1 <= page <= 34:
        raise ValueError("页码超出 GitHub Search 范围")
    return page


def repo_detail(client, full_name, token=None, include_readme=False):
    if (not isinstance(full_name, str) or not REPO_NAME.fullmatch(full_name)
            or full_name.split("/", 1)[1] in {".", ".."}):
        raise ValueError("仓库名称无效")
    path = "/repos/" + full_name
    raw = client.get_json(path, token=token, cache_ttl=86400)
    if not isinstance(raw, dict) or raw.get("private"):
        raise GitHubError("无法读取此公开仓库", 404)
    result = {"full_name": raw.get("full_name", full_name),
              "description": str(raw.get("description") or "")[:1000],
              "forks": int(raw.get("forks_count") or 0),
              "open_issues": int(raw.get("open_issues_count") or 0),
              "license": (raw.get("license") or {}).get("spdx_id") if isinstance(raw.get("license"), dict) else None,
              "created_at": raw.get("created_at"), "updated_at": raw.get("updated_at"),
              "homepage": raw.get("homepage") if isinstance(raw.get("homepage"), str) else "",
              "html_url": f"https://github.com/{full_name}",
              "default_branch": str(raw.get("default_branch") or "main")[:100],
              "readme_url": f"https://github.com/{full_name}#readme"}
    if include_readme:
        try:
            result["readme_html"] = client.get_html(path + "/readme", token=token, cache_ttl=21600)
            match = re.search(r'data-path="([^"]+)"', result["readme_html"][:1000])
            result["readme_path"] = html.unescape(match.group(1))[:300] if match else "README.md"
        except GitHubError as error:
            result["readme_error"] = str(error)
    return result


def metadata():
    return {
        "github_topics": sorted(TOPICS),
        "github_languages": ["TypeScript", "JavaScript", "Python", "Java", "Ruby", "Go", "Rust", "C", "C++", "C#", "PHP", "Swift", "Kotlin", "Dart", "Shell", "HTML", "CSS", "Vue", "Svelte"],
        "star_bands": [band for band, _ in STAR_BANDS],
    }


class GitnoteHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # BaseHTTPRequestHandler logs query strings, including OAuth codes.
        return

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def session_id(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return ""
        return jar[COOKIE_NAME].value if COOKIE_NAME in jar else ""

    def token(self):
        return AUTH.token(self.session_id())

    def rate_snapshot(self):
        return CLIENT.rate_snapshot(AUTH.status(self.session_id())["connected"])

    def request_data(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1_000_000:
            raise ValueError("请求内容过大")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求格式无效")
        return data

    def same_origin(self):
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://127.0.0.1:{self.server.server_port}",
                                        f"http://localhost:{self.server.server_port}"}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/meta":
            return self.send_json(metadata())
        if path == "/api/auth/status":
            return self.send_json(AUTH.status(self.session_id()))
        if path == "/api/rate":
            return self.send_json(self.rate_snapshot())
        if path == "/auth/start":
            try:
                session, url = AUTH.start(self.server.server_port)
            except GitHubError as error:
                return self.send_json({"error": str(error)}, 503)
            return self.redirect(url, f"{COOKIE_NAME}={session}; HttpOnly; SameSite=Lax; Path=/")
        if path == "/auth/callback":
            params = parse_qs(parsed.query)
            if params.get("error"):
                return self.send_bytes("GitHub 授权已取消。请返回发现页。".encode(), "text/plain; charset=utf-8", 400)
            try:
                AUTH.callback(self.session_id(), params.get("state", [""])[0], params.get("code", [""])[0])
            except GitHubError as error:
                return self.send_bytes(str(error).encode(), "text/plain; charset=utf-8", 400)
            return self.redirect("/#feed")
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"),
                 "/vendor/purify.min.js": ("vendor/purify.min.js", "text/javascript; charset=utf-8")}
        if path in files:
            filename, content_type = files[path]
            return self.send_bytes((WEB_DIR / filename).read_bytes(), content_type)
        self.send_json({"error": "未找到页面"}, 404)

    def do_POST(self):
        if not self.same_origin():
            return self.send_json({"error": "请求来源无效"}, 403)
        path = urlparse(self.path).path
        try:
            data = self.request_data()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self.send_json({"error": "请求格式无效"}, 400)
        try:
            if path == "/api/discover":
                profile = clean_profile(data.get("profile"))
                language, topic, star_band = clean_search_filters(data)
                result = discover(CLIENT, profile,
                                  memory=data.get("memory") if isinstance(data.get("memory"), dict) else {},
                                  seen=data.get("seen") if isinstance(data.get("seen"), list) else [],
                                  hidden=data.get("hidden") if isinstance(data.get("hidden"), list) else [],
                                  topic=topic, language=language, star_band=star_band, token=self.token())
                result["rate"] = self.rate_snapshot()
                return self.send_json(result)
            if path == "/api/search":
                keyword = clean_text(data.get("query"), 100)
                if not keyword:
                    return self.send_json({"error": "请输入搜索词"}, 400)
                language, topic, band = clean_search_filters(data)
                sort = clean_text(data.get("sort"), 20) or "best"
                if sort not in {"best", "stars", "updated"}:
                    raise ValueError("排序条件无效")
                result = search_keyword(CLIENT, keyword, self.token(), clean_page(data.get("page", 1)),
                                        language, topic, band, sort)
                return self.send_json({**result, "rate": self.rate_snapshot()})
            if path == "/api/stars-top":
                language, _, _ = clean_search_filters(data)
                result = stars_top(CLIENT, self.token(), clean_page(data.get("page", 1)), language)
                return self.send_json({**result, "rate": self.rate_snapshot()})
            if path == "/api/trending":
                language, _, _ = clean_search_filters(data)
                period = clean_text(data.get("period"), 10) or "daily"
                if period not in {"daily", "weekly", "monthly"}:
                    raise ValueError("Trending 周期无效")
                result = trending_page(CLIENT.cache, period, language)
                return self.send_json({**result, "rate": self.rate_snapshot()})
            if path == "/api/repo-detail":
                return self.send_json(repo_detail(CLIENT, data.get("full_name"), self.token(),
                                                  include_readme=bool(data.get("readme"))))
            if path == "/api/rank":
                items = data.get("items") if isinstance(data.get("items"), list) else []
                return self.send_json({"items": rank_batch(clean_profile(data.get("profile")), items[:500])})
            if path == "/api/taste/signal":
                signal = data.get("signal") if isinstance(data.get("signal"), dict) else {}
                signal = {"kind": signal.get("kind"),
                          "topics": [str(x)[:100] for x in signal.get("topics", [])[:30]] if isinstance(signal.get("topics"), list) else [],
                          "language": clean_text(signal.get("language"), 60) or None,
                          "suppressed": [str(x)[:100] for x in signal.get("suppressed", [])[:20]] if isinstance(signal.get("suppressed"), list) else []}
                return self.send_json({"profile": apply_signal(clean_profile(data.get("profile")), signal)})
            if path == "/api/taste/decay":
                return self.send_json({"profile": apply_session_decay(clean_profile(data.get("profile")))})
            if path == "/api/taste/import-stars":
                token = self.token()
                if not token:
                    return self.send_json({"error": "请先连接 GitHub"}, 401)
                starred = []
                for page in (1, 2):
                    batch = CLIENT.get_json("/user/starred", {"per_page": 100, "page": page}, token=token)
                    if not isinstance(batch, list):
                        break
                    starred.extend({"topics": repo.get("topics") or [], "language": repo.get("language")}
                                   for repo in batch if isinstance(repo, dict)
                                   and ((repo.get("topics") or []) or repo.get("language")))
                    if len(batch) < 100:
                        break
                profile = observe_repos(clean_profile(data.get("profile")), starred)
                for repo in starred:
                    profile = apply_signal(profile, {"kind": "star", **repo})
                top = sorted(profile["topics"], key=lambda t: -profile["topics"][t]["score"])[:5]
                return self.send_json({"profile": profile, "imported": len(starred), "top_topics": top})
            if path == "/api/auth/disconnect":
                AUTH.disconnect(self.session_id())
                return self.send_json({"connected": False})
        except GitHubError as error:
            return self.send_json({"error": str(error), "retry_at": error.retry_at,
                                   "rate": self.rate_snapshot()}, error.status if 400 <= error.status < 600 else 502)
        except ValueError:
            return self.send_json({"error": "请求参数无效"}, 400)
        return self.send_json({"error": "未找到接口"}, 404)


def main():
    parser = argparse.ArgumentParser(description="gitnote GitHub 项目发现")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), GitnoteHandler)
    print(f"gitnote 已启动：http://{args.host}:{server.server_port}", flush=True)
    print("按 Ctrl+C 停止。GitHub 搜索可匿名使用；登录需配置 GH_CLIENT_ID 和 GH_CLIENT_SECRET。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
