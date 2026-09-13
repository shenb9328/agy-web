#!/usr/bin/env python3
"""
Antigravity Web UI (agy-web) Server - Multi-User Isolated Edition
Features:
- Multi-user authentication with isolated storage and execution environments.
- Secure, disk-only salted SHA-256 credentials (never exposed to frontend).
- User 1: shenb (agy9328) -> /home/shenb9328_gmail_com
- User 2: sam (agy93091028) -> /home/shenb9328_gmail_com/.profiles/agy93091028
- Full double-time-descending tree view and session isolation.
"""

import os
import sys
import json
import sqlite3
import uuid
import re
import time
import base64
import hashlib
import shutil
import subprocess
from http.cookies import SimpleCookie
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("AGY_WEB_PORT", 8008))
HOST = os.environ.get("AGY_WEB_HOST", "0.0.0.0")
BASE_DIR = Path(__file__).resolve().parent

# Config paths: prefer ~/.config/agy-web/users.json, fallback to ./users.json
CONFIG_PATHS = [
    Path.home() / ".config" / "agy-web" / "users.json",
    BASE_DIR / "users.json"
]

# Model mapping
MODEL_MAP = {
    "gemini-3.8-flash": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "claude-sonnet-4.6": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4.6": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b": "GPT-OSS 120B (Medium)"
}

# In-memory active sessions: token -> {username, login_time, expires_at}
ACTIVE_SESSIONS = {}
SESSION_EXPIRY_SECONDS = 30 * 86400  # 30 days

# ──────────────────────────────────────────────────────────────────────────────
# User & Configuration Manager
# ──────────────────────────────────────────────────────────────────────────────

def load_auth_config():
    for p in CONFIG_PATHS:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[Agy-Web] Error reading config at {p}: {e}")
    # Default fallback in-memory definition if file not found yet
    return {
        "salt": "agy_web_salt_9328_secure",
        "users": {}
    }

def get_user_profile(username):
    config = load_auth_config()
    users = config.get("users", {})
    return users.get(username)

def hash_password(salt, password):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def verify_credentials(username, password):
    config = load_auth_config()
    salt = config.get("salt", "agy_web_salt_9328_secure")
    user = config.get("users", {}).get(username)
    if not user:
        return False
    expected_hash = user.get("password_hash")
    computed_hash = hash_password(salt, password)
    return computed_hash == expected_hash

# ──────────────────────────────────────────────────────────────────────────────
# Transcript & Database Parser per User Profile
# ──────────────────────────────────────────────────────────────────────────────

def get_summaries_db(user_profile):
    db_path = Path(user_profile["summaries_db"])
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def parse_transcript_file(user_profile, session_id):
    brain_dir = Path(user_profile["brain_dir"])
    log_file = brain_dir / session_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not log_file.exists():
        return []

    messages = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg_type = entry.get("type")
                    content = entry.get("content", "")
                    thinking = entry.get("thinking", "")
                    created_at = entry.get("created_at", "")

                    if msg_type == "USER_INPUT":
                        match = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
                        clean = match.group(1).strip() if match else re.sub(r"<[^>]+>", "", content).strip()
                        messages.append({
                            "role": "user",
                            "content": clean if clean else content.strip(),
                            "thinking": "",
                            "created_at": created_at
                        })
                    elif msg_type == "PLANNER_RESPONSE":
                        if content or thinking:
                            messages.append({
                                "role": "assistant",
                                "content": content,
                                "thinking": thinking,
                                "created_at": created_at
                            })
                except Exception:
                    continue
    except Exception as e:
        print(f"[Agy-Web] Error reading transcript: {e}")

    return messages

# ──────────────────────────────────────────────────────────────────────────────
# Real Live Quota Engine (Direct Google CloudCode API)
# ──────────────────────────────────────────────────────────────────────────────

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CLOUDCODE_BASE = "https://daily-cloudcode-pa.googleapis.com"
ANTIGRAVITY_USER_AGENT = "Mozilla/5.0 Antigravity/1.0.14 Chrome/138.0.7204.235 Electron/37.3.1"
CLIENT_METADATA = json.dumps({"ideType": "ANTIGRAVITY", "platform": "LINUX", "pluginType": "GEMINI"}, separators=(",", ":"))

def get_oauth_credentials():
    cfg = load_auth_config()
    oauth = cfg.get("oauth", {})
    client_id = os.environ.get("ANTIGRAVITY_CLIENT_ID") or oauth.get("client_id")
    client_secret = os.environ.get("ANTIGRAVITY_CLIENT_SECRET") or oauth.get("client_secret")
    if not client_id or not client_secret:
        proxy_path = Path.home() / "antigravity-proxy" / "antigravity_proxy.py"
        if proxy_path.exists():
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("agy_proxy_temp", str(proxy_path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                client_id = client_id or getattr(mod, "CLIENT_ID", None)
                client_secret = client_secret or getattr(mod, "CLIENT_SECRET", None)
            except Exception:
                pass
    return client_id, client_secret

def get_user_live_quota(home_dir):
    token_file = Path(home_dir) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if not token_file.exists():
        token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    if not token_file.exists():
        raise FileNotFoundError(f"未找到 OAuth Token 凭证文件: {token_file}")

    with open(token_file, "r") as f:
        data = json.load(f)

    tok = data.get("token", {})
    access_token = tok.get("access_token")
    refresh_token = tok.get("refresh_token")

    now_ts = time.time()
    expiry_raw = tok.get("expiry")
    expiry_ts = 0.0
    if expiry_raw:
        try:
            s = str(expiry_raw).strip()
            if "." in s:
                head, tail = s.split(".", 1)
                tz_part = ""
                for i, ch in enumerate(tail):
                    if ch in "Z+-":
                        tz_part = tail[i:]
                        tail = tail[:i]
                        break
                s = f"{head}.{tail[:6]}{tz_part}"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            expiry_ts = dt.timestamp()
        except Exception:
            expiry_ts = 0.0

    if not access_token or (expiry_ts - now_ts) < 120:
        if refresh_token:
            client_id, client_secret = get_oauth_credentials()
            if not client_id or not client_secret:
                raise RuntimeError("未配置 OAuth Client ID 或 Client Secret")
            body = json.dumps({
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }).encode()
            req = Request(OAUTH_TOKEN_URL, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urlopen(req, timeout=15) as resp:
                refreshed = json.loads(resp.read().decode())
            access_token = refreshed["access_token"]
            tok["access_token"] = access_token
            if "refresh_token" in refreshed:
                tok["refresh_token"] = refreshed["refresh_token"]
            exp_dt = datetime.now(timezone.utc) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
            tok["expiry"] = exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
            data["token"] = tok
            with open(token_file, "w") as f:
                json.dump(data, f, indent=2)

    url = f"{CLOUDCODE_BASE}/v1internal:fetchAvailableModels"
    req = Request(url, data=b"{}", method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
    req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
    req.add_header("Client-Metadata", CLIENT_METADATA)

    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Request Handler with Multi-User Session Isolation
# ──────────────────────────────────────────────────────────────────────────────

class AgyMultiUserHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_json(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                return {}
            raw = self.rfile.read(content_length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return {}

    def get_session_user(self):
        token = None
        # Check Cookie
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            if "agy_session" in cookie:
                token = cookie["agy_session"].value

        # Check Authorization header fallback
        if not token:
            auth_h = self.headers.get("Authorization", "")
            if auth_h.startswith("Bearer "):
                token = auth_h[7:].strip()

        if not token or token not in ACTIVE_SESSIONS:
            return None

        sess = ACTIVE_SESSIONS[token]
        if time.time() > sess["expires_at"]:
            del ACTIVE_SESSIONS[token]
            return None

        username = sess["username"]
        profile = get_user_profile(username)
        if not profile:
            return None
        return username, profile, token

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.path = "/index.html"
            return super().do_GET()

        # Public / Auth APIs
        if path == "/api/auth/me":
            user_info = self.get_session_user()
            if not user_info:
                self._send_json({"authenticated": False}, status=200)
            else:
                username, profile, _ = user_info
                self._send_json({
                    "authenticated": True,
                    "username": username,
                    "display_name": profile.get("display_name", username),
                    "home": profile.get("home")
                })
            return

        # Protected APIs
        user_info = self.get_session_user()
        if not user_info:
            self._send_json({"error": "Unauthorized", "code": "AUTH_REQUIRED"}, status=401)
            return

        username, profile, _ = user_info

        if path == "/api/tree":
            self.handle_get_tree(profile)
        elif path.startswith("/api/conversations/"):
            conv_id = path.split("/")[-1]
            self.handle_get_conversation_messages(profile, conv_id)
        elif path == "/api/models":
            self.handle_get_models()
        elif path == "/api/quota":
            self.handle_get_quota(profile)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Login
        if path == "/api/auth/login":
            self.handle_login()
            return

        # Logout
        if path == "/api/auth/logout":
            self.handle_logout()
            return

        # Protected APIs
        user_info = self.get_session_user()
        if not user_info:
            self._send_json({"error": "Unauthorized", "code": "AUTH_REQUIRED"}, status=401)
            return

        username, profile, _ = user_info

        if path == "/api/chat/stream":
            self.handle_chat_stream(profile)
        elif path == "/api/conversations":
            self.handle_create_conversation()
        elif path == "/api/projects":
            self.handle_create_project(profile)
        else:
            self._send_json({"error": "Not Found"}, status=404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path

        user_info = self.get_session_user()
        if not user_info:
            self._send_json({"error": "Unauthorized"}, status=401)
            return

        _, profile, _ = user_info

        if path.startswith("/api/conversations/"):
            conv_id = path.split("/")[-1]
            self.handle_update_conversation(profile, conv_id)
        else:
            self._send_json({"error": "Not Found"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path

        user_info = self.get_session_user()
        if not user_info:
            self._send_json({"error": "Unauthorized"}, status=401)
            return

        _, profile, _ = user_info

        if path.startswith("/api/conversations/"):
            conv_id = path.split("/")[-1]
            self.handle_delete_conversation(profile, conv_id)
        else:
            self._send_json({"error": "Not Found"}, status=404)

    # ──────────────────────────────────────────────────────────────────────────
    # Auth Logic
    # ──────────────────────────────────────────────────────────────────────────

    def handle_login(self):
        data = self._read_json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not username or not password:
            self._send_json({"error": "用户名和密码不能为空"}, status=400)
            return

        if not verify_credentials(username, password):
            self._send_json({"error": "用户名或密码不正确"}, status=401)
            return

        profile = get_user_profile(username)
        token = uuid.uuid4().hex
        expires_at = time.time() + SESSION_EXPIRY_SECONDS

        ACTIVE_SESSIONS[token] = {
            "username": username,
            "login_time": time.time(),
            "expires_at": expires_at
        }

        # Issue HTTPOnly Cookie
        cookie = f"agy_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_EXPIRY_SECONDS}"
        self._send_json({
            "success": True,
            "token": token,
            "username": username,
            "display_name": profile.get("display_name", username)
        }, headers={"Set-Cookie": cookie})

    def handle_logout(self):
        user_info = self.get_session_user()
        if user_info:
            _, _, token = user_info
            if token in ACTIVE_SESSIONS:
                del ACTIVE_SESSIONS[token]

        # Expire Cookie
        cookie = "agy_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        self._send_json({"success": True}, headers={"Set-Cookie": cookie})

    # ──────────────────────────────────────────────────────────────────────────
    # Project & Conversation Tree Handler (Isolated per user profile)
    # ──────────────────────────────────────────────────────────────────────────

    def handle_get_tree(self, user_profile):
        projects_map = {}
        conversations = []
        db_path = Path(user_profile["summaries_db"])

        if db_path.exists():
            try:
                conn = get_summaries_db(user_profile)
                if conn:
                    query = """
                        SELECT conversation_id, title, preview, step_count, last_modified_time, 
                               workspace_uris, project_id, status
                        FROM conversation_summaries 
                        WHERE killed = 0
                        ORDER BY last_modified_time DESC
                    """
                    rows = conn.execute(query).fetchall()
                    conn.close()

                    user_home = user_profile.get("home", str(Path.home()))

                    for r in rows:
                        conv_id = r["conversation_id"]
                        title = r["title"] or "无标题会话"
                        raw_w = r["workspace_uris"]
                        raw_p = r["project_id"] or "default-cli-project"
                        mtime = r["last_modified_time"]

                        proj_id = "default"
                        proj_name = "默认工作区"
                        proj_dir = user_home

                        if raw_w:
                            try:
                                uris = json.loads(raw_w)
                                if uris and isinstance(uris, list) and uris[0]:
                                    clean_path = unquote(uris[0].replace("file://", "")).rstrip("/")
                                    proj_dir = clean_path
                                    proj_name = clean_path.split("/")[-1] if "/" in clean_path else clean_path
                                    proj_id = f"proj-{proj_name}"
                            except Exception:
                                pass
                        elif raw_p == "outside-of-project":
                            proj_id = "outside"
                            proj_name = "独立任务/未归类"
                            proj_dir = user_home
                        elif raw_p != "default-cli-project":
                            proj_id = raw_p
                            proj_name = raw_p

                        if proj_id not in projects_map:
                            projects_map[proj_id] = {
                                "id": proj_id,
                                "name": proj_name,
                                "path": proj_dir,
                                "count": 0,
                                "latest_time": mtime
                            }
                        projects_map[proj_id]["count"] += 1
                        if mtime > projects_map[proj_id].get("latest_time", ""):
                            projects_map[proj_id]["latest_time"] = mtime

                        conversations.append({
                            "id": conv_id,
                            "folder_id": proj_id,
                            "title": title,
                            "preview": r["preview"],
                            "updated_at": mtime
                        })
            except Exception as e:
                print(f"[Agy-Web] Error querying user db: {e}")

        # Ensure default project exists even if 0 conversations
        if "default" not in projects_map:
            projects_map["default"] = {
                "id": "default",
                "name": "默认工作区",
                "path": user_profile.get("home", str(Path.home())),
                "count": 0,
                "latest_time": "2026-01-01"
            }

        # Sort projects strictly by latest_time descending
        projects_list = list(projects_map.values())
        projects_list.sort(key=lambda p: p.get("latest_time", ""), reverse=True)

        self._send_json({
            "folders": projects_list,
            "conversations": conversations
        })

    def handle_get_conversation_messages(self, user_profile, conv_id):
        messages = parse_transcript_file(user_profile, conv_id)
        title = "会话详情"
        db_path = Path(user_profile["summaries_db"])

        if db_path.exists():
            try:
                conn = get_summaries_db(user_profile)
                if conn:
                    row = conn.execute("SELECT title FROM conversation_summaries WHERE conversation_id = ?", (conv_id,)).fetchone()
                    conn.close()
                    if row and row["title"]:
                        title = row["title"]
            except Exception:
                pass

        self._send_json({
            "conversation": {
                "id": conv_id,
                "title": title
            },
            "messages": messages
        })

    def handle_create_project(self, user_profile):
        data = self._read_json()
        name = data.get("name", "").strip()
        if not name:
            self._send_json({"error": "项目名称不能为空"}, status=400)
            return

        base_home = Path(user_profile.get("home", str(Path.home())))
        proj_path = base_home / name
        try:
            proj_path.mkdir(parents=True, exist_ok=True)
            self._send_json({
                "id": f"proj-{name}",
                "name": name,
                "path": str(proj_path)
            })
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def handle_create_conversation(self):
        data = self._read_json()
        folder_id = data.get("folder_id", "default")
        title = data.get("title", "新会话").strip()
        new_id = str(uuid.uuid4())
        self._send_json({
            "id": new_id,
            "folder_id": folder_id,
            "title": title,
            "is_new": True
        })

    def handle_update_conversation(self, user_profile, conv_id):
        data = self._read_json()
        new_title = data.get("title")
        new_proj_path = data.get("project_path")
        db_path = Path(user_profile["summaries_db"])

        if db_path.exists():
            try:
                conn = get_summaries_db(user_profile)
                if conn:
                    with conn:
                        if new_title:
                            conn.execute("UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?", (new_title.strip(), conv_id))
                        if new_proj_path:
                            uri = f"file://{new_proj_path}"
                            conn.execute("UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?", (json.dumps([uri]), conv_id))
                    conn.close()
            except Exception as e:
                print(f"[Agy-Web] Error updating conversation: {e}")
        self._send_json({"success": True})

    def handle_delete_conversation(self, user_profile, conv_id):
        db_path = Path(user_profile["summaries_db"])
        if db_path.exists():
            try:
                conn = get_summaries_db(user_profile)
                if conn:
                    with conn:
                        conn.execute("UPDATE conversation_summaries SET killed = 1 WHERE conversation_id = ?", (conv_id,))
                    conn.close()
            except Exception:
                pass
        self._send_json({"success": True})

    def handle_get_models(self):
        models = [
            {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash (High)", "desc": "极速响应，百万上下文"},
            {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)", "desc": "平衡速度与推理"},
            {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro (High)", "desc": "高阶代码与深度推演"},
            {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6 (Thinking)", "desc": "思考模式，高品质代码"},
            {"id": "claude-opus-4.6", "name": "Claude Opus 4.6 (Thinking)", "desc": "超高智力旗舰"},
            {"id": "gpt-oss-120b", "name": "GPT-OSS 120B (Medium)", "desc": "开源旗舰大模型"}
        ]
        self._send_json({"data": models})

    def handle_get_quota(self, user_profile):
        try:
            home_dir = user_profile.get("home", str(Path.home()))
            payload = get_user_live_quota(home_dir)
            models = payload.get("models", {})
            now_utc = datetime.now(timezone.utc)

            displayed_models = [
                {
                    "name": "Gemini 3.8 Flash",
                    "desc": "极速响应，百万上下文",
                    "key": "gemini-3-flash",
                    "pool": "Gemini 共享配额池"
                },
                {
                    "name": "Gemini 3.1 Pro",
                    "desc": "高阶代码与深度推演",
                    "key": "gemini-3.1-pro-low",
                    "pool": "Gemini 共享配额池"
                },
                {
                    "name": "Claude Sonnet 4.6",
                    "desc": "思考模式，高品质代码",
                    "key": "claude-sonnet-4-6",
                    "pool": "Claude 独立配额池"
                },
                {
                    "name": "Claude Opus 4.6",
                    "desc": "超高智力旗舰",
                    "key": "claude-opus-4-6-thinking",
                    "pool": "Claude 独立配额池"
                },
                {
                    "name": "GPT-OSS 120B",
                    "desc": "开源旗舰大模型",
                    "key": "gpt-oss-120b-medium",
                    "pool": "开源模型独立池"
                }
            ]

            result_models = []
            for item in displayed_models:
                info = models.get(item["key"], {})
                q = info.get("quotaInfo", {})
                rem = q.get("remainingFraction")
                reset_str = q.get("resetTime")

                percentage = f"{rem * 100:.1f}%" if rem is not None else "100.0%"
                fraction = rem if rem is not None else 1.0

                time_desc = "循环自动重置"
                if reset_str:
                    try:
                        dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                        delta = dt - now_utc
                        total_sec = int(delta.total_seconds())
                        if total_sec > 0:
                            mins = total_sec // 60
                            secs = total_sec % 60
                            time_desc = f"约 {mins} 分 {secs} 秒后重置"
                        else:
                            time_desc = "即将循环重置"
                    except Exception:
                        time_desc = reset_str

                result_models.append({
                    "name": item["name"],
                    "desc": item["desc"],
                    "pool": item["pool"],
                    "percentage": percentage,
                    "fraction": fraction,
                    "reset_time": reset_str,
                    "reset_desc": time_desc
                })

            tier_name = payload.get("currentTier", {}).get("name") or user_profile.get("display_name", "Antigravity")

            self._send_json({
                "success": True,
                "tier": tier_name,
                "models": result_models
            })
        except Exception as e:
            print(f"[Agy-Web] Error fetching live quota: {e}")
            self._send_json({
                "success": False,
                "error": f"获取官方实时配额失败: {str(e)}"
            }, status=500)

    # ──────────────────────────────────────────────────────────────────────────
    # Direct agy CLI Execution (Isolated per user profile)
    # ──────────────────────────────────────────────────────────────────────────

    def handle_chat_stream(self, user_profile):
        data = self._read_json()
        conv_id = data.get("conversation_id")
        user_message = data.get("message", "").strip()
        requested_model = data.get("model", "gemini-3.8-flash")
        project_dir = data.get("project_dir")

        if not user_message:
            self._send_json({"error": "消息内容不能为空"}, status=400)
            return

        cli_model = MODEL_MAP.get(requested_model, "Gemini 3.8 Flash (High)")
        agy_bin = user_profile.get("bin", "/usr/local/bin/agy")
        user_home = user_profile.get("home", str(Path.home()))
        brain_dir = Path(user_profile.get("brain_dir", str(Path.home() / ".gemini" / "antigravity-cli" / "brain")))

        # Prepare SSE Stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        existing_session = False
        if conv_id and (brain_dir / conv_id).exists():
            existing_session = True

        cmd = [
            agy_bin,
            "-p", user_message,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--model", cli_model
        ]

        if existing_session:
            cmd.extend(["--conversation", conv_id])

        work_dir = Path(user_home)
        if project_dir and Path(project_dir).exists():
            work_dir = Path(project_dir)

        # Isolated environment variables
        env = os.environ.copy()
        env["HOME"] = user_home
        env["USER"] = user_profile.get("display_name", "user").split()[0]

        real_conv_id = conv_id
        full_content = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(work_dir),
                env=env
            )

            init_evt = json.dumps({"type": "init", "conversation_id": conv_id})
            self.wfile.write(f"data: {init_evt}\n\n".encode("utf-8"))
            self.wfile.flush()

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event_data = json.loads(line)
                    evt_name = event_data.get("event")

                    if evt_name == "init":
                        assigned_id = event_data.get("conversation_id")
                        if assigned_id:
                            real_conv_id = assigned_id
                    elif evt_name == "step_update":
                        su = event_data.get("step_update", {})
                        delta = su.get("text_delta", "")
                        if delta:
                            full_content.append(delta)
                            out_evt = json.dumps({"type": "delta", "content": delta, "thinking": ""})
                            self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
                            self.wfile.flush()
                    elif evt_name == "result":
                        res_obj = event_data.get("result", {})
                        if not full_content and res_obj.get("response"):
                            resp_text = res_obj.get("response")
                            full_content.append(resp_text)
                            out_evt = json.dumps({"type": "delta", "content": resp_text, "thinking": ""})
                            self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
                            self.wfile.flush()
                except Exception:
                    continue

            proc.wait()

            # Ensure new session has correct workspace uri recorded
            db_path = Path(user_profile["summaries_db"])
            if not existing_session and real_conv_id and db_path.exists():
                try:
                    conn = get_summaries_db(user_profile)
                    if conn:
                        with conn:
                            uri = f"file://{work_dir}"
                            conn.execute("UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?",
                                         (json.dumps([uri]), real_conv_id))
                        conn.close()
                except Exception as err:
                    print(f"[Agy-Web] Error setting workspace uri: {err}")
        except Exception as e:
            err_msg = f"\n\n*(调用 {agy_bin} 失败: {str(e)})*"
            out_evt = json.dumps({"type": "delta", "content": err_msg, "thinking": ""})
            self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
            self.wfile.flush()

        done_evt = json.dumps({"type": "done", "conversation_id": real_conv_id})
        self.wfile.write(f"data: {done_evt}\n\n".encode("utf-8"))
        self.wfile.flush()

# ──────────────────────────────────────────────────────────────────────────────
# Main Execution
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print(f"🚀 [Agy-Web] Starting Multi-User Secure Native Web UI on http://{HOST}:{PORT}")
    cfg = load_auth_config()
    configured_users = list(cfg.get("users", {}).keys())
    print(f"👥 [Agy-Web] Configured isolated users: {configured_users}")

    server = ThreadingHTTPServer((HOST, PORT), AgyMultiUserHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 [Agy-Web] Shutting down.")
        server.server_close()

if __name__ == "__main__":
    main()
