"""GitHub OAuth App authorization-code flow; credentials stay in process memory."""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from github_client import GitHubError


COOKIE_NAME = "gw_session"


class AuthManager:
    def __init__(self, client, client_id=None, client_secret=None):
        self.client = client
        self.client_id = client_id if client_id is not None else os.getenv("GH_CLIENT_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.getenv("GH_CLIENT_SECRET", "")
        self.sessions = {}
        self.lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    def start(self, port):
        if not self.configured:
            raise GitHubError("请先配置 GH_CLIENT_ID 和 GH_CLIENT_SECRET")
        session = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
        with self.lock:
            self.sessions[session] = {"state": state, "verifier": verifier, "started": time.time(),
                                      "redirect_uri": redirect_uri, "token": None, "login": None}
        query = urlencode({"client_id": self.client_id, "redirect_uri": redirect_uri,
                           "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return session, "https://github.com/login/oauth/authorize?" + query

    def callback(self, session, state, code):
        with self.lock:
            entry = self.sessions.get(session)
            if (not entry or not state or not secrets.compare_digest(state, entry["state"])
                    or time.time() - entry["started"] > 600):
                raise GitHubError("登录校验失败，请重新连接")
            # One-time state even when exchange fails.
            entry["state"] = ""
            verifier = entry["verifier"]
            redirect_uri = entry["redirect_uri"]
        body = urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                          "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}).encode()
        request = Request("https://github.com/login/oauth/access_token", body,
                          {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                           "User-Agent": "gitnote-local"})
        try:
            with urlopen(request, timeout=12) as response:
                data = json.loads(response.read(100_000))
        except (HTTPError, URLError, ValueError) as error:
            raise GitHubError("GitHub 登录交换失败") from error
        token = data.get("access_token")
        if not token:
            raise GitHubError("GitHub 未返回访问凭据，请重试")
        granted = {scope.strip() for scope in str(data.get("scope") or "").split(",") if scope.strip()}
        if granted - {"read:user"}:
            raise GitHubError("GitHub 返回了超出公开读取需求的旧授权。请在 GitHub 撤销该应用授权后重试")
        user = self.client.get_json("/user", token=token)
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise GitHubError("无法验证 GitHub 身份")
        with self.lock:
            entry["token"] = token
            entry["login"] = user["login"]
            entry["verifier"] = ""
        return user["login"]

    def token(self, session):
        with self.lock:
            return self.sessions.get(session, {}).get("token")

    def status(self, session):
        with self.lock:
            entry = self.sessions.get(session, {})
            return {"configured": self.configured, "connected": bool(entry.get("token")),
                    "login": entry.get("login")}

    def disconnect(self, session):
        with self.lock:
            self.sessions.pop(session, None)
