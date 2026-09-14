#!/usr/bin/env python3
"""
Antigravity Web UI (agy-web) Server - Dual-Mode Remote Synced Edition
Features:
- Dual-Mode Operation:
  1. Live Bidirectional RPC Mode: Directly bridges to Antigravity Language Server (LS),
     synchronizing conversations bidirectionally with Google Antigravity Remote (https://antigravity.google.com) in real-time.
  2. Offline CLI Fallback Mode: Seamlessly falls back to local `agy -p` streaming pipeline
     when Language Server daemon is offline.
- Multi-user authentication with isolated storage and execution environments.
- Secure, disk-only salted SHA-256 credentials (never exposed to frontend).
- Single source of truth: SQLite + Language Server memory reconciliation.
- Pure Python 3 standard library: Zero third-party pip dependencies, ~15MB RAM.
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
# Language Server Bridge (Connect-RPC Dual-Way Client)
# ──────────────────────────────────────────────────────────────────────────────

class AntigravityLSBridge:
    """
    Lightweight client for Antigravity Language Server (Connect-RPC protocol).
    Communicates directly with the resident daemon (e.g. PID running `agy remote-control serve`)
    to achieve full bidirectional synchronization with Google Antigravity Remote.
    """
    def __init__(self):
        self.cached_instance = None
        self.last_check_time = 0
        self.check_interval = 5.0  # seconds
        self.service_prefix = "exa.language_server_pb.LanguageServerService"

    def get_instance(self, force_refresh=False):
        now = time.time()
        if not force_refresh and self.cached_instance and (now - self.last_check_time < self.check_interval):
            return self.cached_instance

        inst = self._discover()
        if inst and self._health_check(inst):
            self.cached_instance = inst
            self.last_check_time = now
            return inst

        self.cached_instance = None
        self.last_check_time = now
        return None

    def _discover(self):
        # 1. Environment variables (highest priority)
        env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
        env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
        if env_addr and env_csrf:
            host, port = env_addr.split(":") if ":" in env_addr else ("127.0.0.1", env_addr)
            return {
                "host": host,
                "port": int(port),
                "csrfToken": env_csrf,
                "source": "environment_variable"
            }

        # 2. Scrape discovery files
        search_dirs = [
            Path.home() / ".gemini" / "antigravity-cli" / "daemon",
            Path.home() / ".gemini" / "antigravity" / "daemon",
            Path.home() / ".gemini" / "antigravity-ide" / "daemon",
            Path.home() / ".config" / "agy-web"
        ]

        candidates = []
        for d in search_dirs:
            if not d.exists():
                continue
            # ls_*.json files
            for f in sorted(d.glob("ls_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                candidates.append(f)
            # ls.json file
            ls_direct = d / "ls.json"
            if ls_direct.exists():
                candidates.append(ls_direct)

        for filepath in candidates:
            try:
                with open(filepath, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    port = data.get("httpPort") or data.get("httpsPort")
                    csrf = data.get("csrfToken")
                    if port and csrf:
                        return {
                            "host": "127.0.0.1",
                            "port": int(port),
                            "csrfToken": csrf,
                            "pid": data.get("pid"),
                            "source": f"file:{filepath.name}"
                        }
            except Exception:
                continue

        return None

    def _health_check(self, inst):
        try:
            res = self.call_rpc("GetWorkspaceInfos", {}, inst=inst, timeout=2.0)
            return res is not None and "code" not in res
        except Exception:
            return False

    def call_rpc(self, method, payload, inst=None, timeout=10.0):
        target = inst or self.get_instance()
        if not target:
            return None

        url = f"http://{target['host']}:{target['port']}/{self.service_prefix}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-codeium-csrf-token", target["csrfToken"])

        try:
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except Exception as e:
            if hasattr(e, "read"):
                try:
                    return json.loads(e.read().decode("utf-8"))
                except Exception:
                    pass
            return None

    def get_metadata(self):
        return {
            "ideName": "antigravity",
            "ideVersion": "2.13.0",
            "extensionVersion": "2.13.0",
            "allowFileAccess": True,
            "allWorkspaceTrustGranted": True
        }

    def resolve_plan_model(self, model_name):
        if not model_name:
            return "MODEL_GOOGLE_GEMINI_2_5_FLASH"
        m = str(model_name).lower().strip()
        if "pro" in m:
            return "MODEL_GOOGLE_GEMINI_2_5_PRO"
        elif "gpt" in m or "oss" in m:
            return "MODEL_OPENAI_GPT_OSS_120B_MEDIUM"
        elif "lite" in m:
            return "MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE"
        elif "thinking" in m:
            return "MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING"
        else:
            return "MODEL_GOOGLE_GEMINI_2_5_FLASH"

    def start_cascade(self, workspace_path=None, cascade_id=None):
        payload = {
            "metadata": self.get_metadata(),
            "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT"
        }
        if cascade_id:
            payload["cascadeId"] = str(cascade_id)
        if workspace_path:
            uri = f"file://{workspace_path}"
            payload["workspaceFolderAbsoluteUri"] = uri
            payload["workspaceUris"] = [uri]

        res = self.call_rpc("StartCascade", payload)
        if res and isinstance(res, dict):
            if "cascadeId" in res:
                return res["cascadeId"]
            elif cascade_id and "already exists" in res.get("message", ""):
                return cascade_id
        return None

    def send_user_message(self, cascade_id, message, model=None):
        plan_model = self.resolve_plan_model(model)
        payload = {
            "metadata": self.get_metadata(),
            "cascadeId": cascade_id,
            "items": [{"text": message}],
            "cascadeConfig": {
                "plannerConfig": {
                    "planModel": plan_model
                }
            }
        }
        return self.call_rpc("SendUserCascadeMessage", payload)

    def handle_user_interaction(self, cascade_id, trajectory_id, step_index, allow=True, scope="PERMISSION_SCOPE_ONCE", deny_message="用户在Web端取消了该操作"):
        payload = {
            "cascadeId": str(cascade_id),
            "interaction": {
                "trajectoryId": str(trajectory_id),
                "stepIndex": int(step_index),
                "permission": {
                    "allow": bool(allow),
                    "scope": scope if allow else "PERMISSION_SCOPE_ONCE",
                    "userDenyInstruction": "" if allow else (deny_message or "用户拒绝了执行")
                }
            }
        }
        return self.call_rpc("HandleCascadeUserInteraction", payload)

    def get_trajectory_steps(self, cascade_id, step_offset=0):
        payload = {
            "cascadeId": cascade_id,
            "stepOffset": step_offset
        }
        return self.call_rpc("GetCascadeTrajectorySteps", payload)

    def get_trajectory(self, cascade_id):
        return self.call_rpc("GetCascadeTrajectory", {"cascadeId": cascade_id})

    def get_all_trajectories(self):
        return self.call_rpc("GetAllCascadeTrajectories", {})

ls_bridge = AntigravityLSBridge()

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
    db_path = Path(user_profile.get("summaries_db", str(Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")))
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def parse_transcript_file(user_profile, session_id):
    brain_dir = Path(user_profile.get("brain_dir", str(Path.home() / ".gemini" / "antigravity-cli" / "brain")))
    log_file = brain_dir / session_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not log_file.exists():
        # Fallback: check live steps via Language Server if available
        inst = ls_bridge.get_instance()
        if inst:
            traj_data = ls_bridge.get_trajectory(session_id)
            if traj_data and "trajectory" in traj_data:
                steps = traj_data.get("trajectory", {}).get("steps", [])
                messages = []
                for s in steps:
                    stype = s.get("type")
                    if stype == "CORTEX_STEP_TYPE_USER_INPUT":
                        content = s.get("userInput", {}).get("text", "")
                        messages.append({
                            "role": "user",
                            "content": content,
                            "thinking": "",
                            "created_at": s.get("metadata", {}).get("createdAt", "")
                        })
                    elif stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                        pr = s.get("plannerResponse", {})
                        resp_text = pr.get("response") or pr.get("content") or ""
                        thinking = pr.get("thinking") or ""
                        if resp_text or thinking:
                            messages.append({
                                "role": "assistant",
                                "content": resp_text,
                                "thinking": thinking,
                                "created_at": s.get("metadata", {}).get("createdAt", "")
                            })
                if messages:
                    return messages
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
                tail = tail[:6].ljust(6, "0")
                s = f"{head}.{tail}{tz_part}"
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            expiry_ts = dt.timestamp()
        except Exception:
            expiry_ts = 0.0

    if not access_token or (expiry_ts > 0 and now_ts >= (expiry_ts - 300)):
        client_id, client_secret = get_oauth_credentials()
        if not client_id or not client_secret:
            raise ValueError("Token 已过期，且系统未配置 ANTIGRAVITY_CLIENT_ID / CLIENT_SECRET 自动刷新凭据")

        body_data = json.dumps({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }).encode("utf-8")

        req = Request(OAUTH_TOKEN_URL, data=body_data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            refresh_resp = json.loads(resp.read().decode("utf-8"))

        access_token = refresh_resp["access_token"]
        expires_in = refresh_resp.get("expires_in", 3600)
        new_expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        tok["access_token"] = access_token
        tok["expiry"] = new_expiry_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        data["token"] = tok

        try:
            with open(token_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as err:
            print(f"[Agy-Web] Warning: failed to save refreshed token: {err}")

    # Fetch available models & quotas
    req = Request(f"{CLOUDCODE_BASE}/v1internal:fetchAvailableModels", data=b"{}", method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
    req.add_header("X-Goog-Api-Client", "gl-node/unknown fire/unknown")
    req.add_header("X-Client-Metadata", CLIENT_METADATA)

    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# ──────────────────────────────────────────────────────────────────────────────
# Web Request Handler
# ──────────────────────────────────────────────────────────────────────────────

class AgyMultiUserHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_json(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {}

    def get_session_user(self):
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None

        if "agy_session" not in cookie:
            return None

        token = cookie["agy_session"].value
        session = ACTIVE_SESSIONS.get(token)
        if not session:
            return None

        if time.time() > session.get("expires_at", 0):
            del ACTIVE_SESSIONS[token]
            return None

        username = session.get("username")
        profile = get_user_profile(username)
        if not profile:
            return None

        return username, profile, token

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Public resources (including PWA manifest, service worker, icons)
        if path in ["/", "/index.html", "/favicon.ico", "/manifest.json", "/sw.js"] or path.startswith("/icons/"):
            if path == "/sw.js":
                sw_path = BASE_DIR / "sw.js"
                if sw_path.exists():
                    body = sw_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Service-Worker-Allowed", "/")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    self.wfile.write(body)
                    return
            elif path == "/manifest.json":
                manifest_path = BASE_DIR / "manifest.json"
                if manifest_path.exists():
                    body = manifest_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    self.wfile.write(body)
                    return
            super().do_GET()
            return

        # Check bridge status (public or authenticated)
        if path == "/api/bridge/status":
            self.handle_get_bridge_status()
            return

        # Auth status check
        if path in ["/api/auth/me", "/api/auth/status"]:
            user_info = self.get_session_user()
            if user_info:
                username, profile, _ = user_info
                self._send_json({
                    "authenticated": True,
                    "username": username,
                    "display_name": profile.get("display_name", username),
                    "home": profile.get("home")
                })
            else:
                self._send_json({"authenticated": False})
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
        elif path == "/api/cascade/interact":
            self.handle_cascade_interact()
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

    def handle_get_bridge_status(self):
        inst = ls_bridge.get_instance()
        if inst:
            self._send_json({
                "online": True,
                "mode": "rpc_dual_sync",
                "mode_text": "双向同步已激活 (Remote Synced)",
                "port": inst.get("port"),
                "source": inst.get("source"),
                "description": "已直连本地 Antigravity Language Server，Web 与 Google Antigravity Remote 实时完全双向同步！"
            })
        else:
            self._send_json({
                "online": False,
                "mode": "cli_standalone",
                "mode_text": "本地直驱模式 (Standalone CLI)",
                "description": "未检测到后台活跃的 Language Server，自动降级为原生 CLI 管道直驱模式。"
            })

    # ──────────────────────────────────────────────────────────────────────────
    # Project & Conversation Tree Handler (Dual Mode Reconciliation)
    # ──────────────────────────────────────────────────────────────────────────────

    def handle_get_tree(self, user_profile):
        projects_map = {}
        conversations = []
        seen_conv_ids = set()

        # 1. 优先注入正在运行中的活跃 Language Server 会话（实现 Remote 创建的会话秒同步）
        inst = ls_bridge.get_instance()
        if inst:
            try:
                trajectories_resp = ls_bridge.get_all_trajectories()
                if trajectories_resp and "trajectorySummaries" in trajectories_resp:
                    for conv_id, summary in trajectories_resp["trajectorySummaries"].items():
                        title = summary.get("summary") or summary.get("annotations", {}).get("title") or "无标题会话"
                        mtime = summary.get("lastModifiedTime") or datetime.now(timezone.utc).isoformat()
                        status = summary.get("status", "")

                        # Workspace extraction
                        proj_id = "default"
                        proj_name = "默认工作区"
                        proj_dir = user_profile.get("home", str(Path.home()))

                        ws_list = summary.get("workspaces", [])
                        if ws_list and isinstance(ws_list, list) and ws_list[0].get("workspaceFolderAbsoluteUri"):
                            uri = ws_list[0]["workspaceFolderAbsoluteUri"]
                            clean_path = unquote(uri.replace("file://", "")).rstrip("/")
                            proj_dir = clean_path
                            proj_name = clean_path.split("/")[-1] if "/" in clean_path else clean_path
                            proj_id = f"proj-{proj_name}"

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

                        seen_conv_ids.add(conv_id)
                        conversations.append({
                            "id": conv_id,
                            "folder_id": proj_id,
                            "title": f"⚡ {title}" if status == "CASCADE_RUN_STATUS_RUNNING" else title,
                            "preview": title,
                            "updated_at": mtime,
                            "is_live": True
                        })
            except Exception as e:
                print(f"[Agy-Web] Warning merging live trajectories: {e}")

        # 2. 从本地 SQLite 数据库中读取全量持久化历史
        db_path = Path(user_profile.get("summaries_db", str(Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")))
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
                        if conv_id in seen_conv_ids:
                            continue  # 已被 Live 状态置顶覆盖

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
                            "updated_at": mtime,
                            "is_live": False
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
                "latest_time": ""
            }

        # Sort projects by latest_time descending
        sorted_folders = sorted(
            projects_map.values(),
            key=lambda x: (x["latest_time"] or ""),
            reverse=True
        )

        # Sort conversations by updated_at descending
        sorted_convs = sorted(
            conversations,
            key=lambda x: (x["updated_at"] or ""),
            reverse=True
        )

        self._send_json({
            "folders": sorted_folders,
            "conversations": sorted_convs
        })

    def handle_get_conversation_messages(self, user_profile, conv_id):
        messages = parse_transcript_file(user_profile, conv_id)
        self._send_json({
            "conversation_id": conv_id,
            "messages": messages
        })

    def handle_create_conversation(self):
        new_id = str(uuid.uuid4())
        self._send_json({"conversation_id": new_id})

    def handle_create_project(self, user_profile):
        data = self._read_json()
        proj_name = data.get("name", "").strip()
        proj_path = data.get("path", "").strip()

        if not proj_name or not proj_path:
            self._send_json({"error": "项目名称和物理路径不能为空"}, status=400)
            return

        target_dir = Path(proj_path)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            self._send_json({
                "success": True,
                "project": {
                    "id": f"proj-{proj_name}",
                    "name": proj_name,
                    "path": str(target_dir)
                }
            })
        except Exception as e:
            self._send_json({"error": f"无法创建物理目录: {str(e)}"}, status=500)

    def handle_update_conversation(self, user_profile, conv_id):
        data = self._read_json()
        new_title = data.get("title", "").strip()
        new_folder_id = data.get("folder_id")

        if not new_title and not new_folder_id:
            self._send_json({"error": "没有可更新的字段"}, status=400)
            return

        db_path = Path(user_profile.get("summaries_db", str(Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")))
        if db_path.exists():
            try:
                conn = get_summaries_db(user_profile)
                if conn:
                    with conn:
                        if new_title:
                            conn.execute("UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                                         (new_title, conv_id))
                    conn.close()
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
                return

        self._send_json({"success": True})

    def handle_delete_conversation(self, user_profile, conv_id):
        db_path = Path(user_profile.get("summaries_db", str(Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")))
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

    def handle_cascade_interact(self):
        body = self._read_json() or {}
        cascade_id = body.get("cascade_id")
        trajectory_id = body.get("trajectory_id")
        step_index = body.get("step_index")
        allow = body.get("allow", True)
        scope = body.get("scope", "PERMISSION_SCOPE_ONCE")
        deny_msg = body.get("deny_message", "用户在Web界面拒绝了此命令")

        if not cascade_id or not trajectory_id or step_index is None:
            self._send_json({"error": "Missing parameters", "success": False}, status=400)
            return

        res = ls_bridge.handle_user_interaction(
            cascade_id=cascade_id,
            trajectory_id=trajectory_id,
            step_index=step_index,
            allow=allow,
            scope=scope,
            deny_message=deny_msg
        )
        self._send_json({"success": True, "result": res})

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

            self._send_json({"data": result_models})
        except Exception as e:
            self._send_json({
                "success": False,
                "error": f"获取官方实时配额失败: {str(e)}"
            }, status=500)

    # ──────────────────────────────────────────────────────────────────────────
    # Dual-Mode Chat Stream: Live RPC Synced vs Native CLI Pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def handle_chat_stream(self, user_profile):
        data = self._read_json()
        conv_id = data.get("conversation_id")
        user_message = data.get("message", "").strip()
        requested_model = data.get("model", "gemini-3.8-flash")
        project_dir = data.get("project_dir")
        auto_approve = bool(data.get("auto_approve", False))

        if not user_message:
            self._send_json({"error": "消息内容不能为空"}, status=400)
            return

        cli_model = MODEL_MAP.get(requested_model, "Gemini 3.8 Flash (High)")
        agy_bin = user_profile.get("bin", "/home/shenb/.local/bin/agy")
        user_home = user_profile.get("home", str(Path.home()))
        brain_dir = Path(user_profile.get("brain_dir", str(Path.home() / ".gemini" / "antigravity-cli" / "brain")))

        work_dir = Path(user_home)
        if project_dir and Path(project_dir).exists():
            work_dir = Path(project_dir)

        # Prepare SSE Stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        # Check if Language Server Bridge is active (Dual-Way RPC mode)
        ls_inst = ls_bridge.get_instance()

        # ======================================================================
        # MODE 1: Live Bidirectional RPC Mode (Synchronized with Google Remote)
        # ======================================================================
        if ls_inst:
            try:
                real_conv_id = conv_id
                if not real_conv_id or real_conv_id.startswith("temp-"):
                    new_cid = str(uuid.uuid4())
                    real_conv_id = ls_bridge.start_cascade(str(work_dir), cascade_id=new_cid) or new_cid
                else:
                    traj_check = ls_bridge.get_trajectory(real_conv_id)
                    if not traj_check or "trajectory" not in traj_check:
                        ls_bridge.start_cascade(str(work_dir), cascade_id=real_conv_id)

                # Push init event with actual conversation ID
                init_evt = json.dumps({"type": "init", "conversation_id": real_conv_id})
                self.wfile.write(f"data: {init_evt}\n\n".encode("utf-8"))
                self.wfile.flush()

                # Get initial step count offset
                traj_info = ls_bridge.get_trajectory(real_conv_id)
                start_offset = 0
                if traj_info and isinstance(traj_info, dict) and "trajectory" in traj_info:
                    start_offset = len(traj_info.get("trajectory", {}).get("steps", []))

                # Inject message into Language Server memory (instantly syncs to Google Remote!)
                chosen_model = requested_model
                ls_bridge.send_user_message(real_conv_id, user_message, model=chosen_model)

                # Poll for steps and stream response deltas
                current_offset = start_offset
                step_streamed_lens = {}
                is_done = False
                poll_start = time.time()
                empty_polls = 0
                retried_with_flash = False

                while not is_done and (time.time() - poll_start < 300):
                    time.sleep(0.2)
                    steps_resp = ls_bridge.get_trajectory_steps(real_conv_id, current_offset)
                    if not steps_resp or not isinstance(steps_resp, dict):
                        empty_polls += 1
                        if empty_polls > 40:
                            break
                        continue

                    steps = steps_resp.get("steps", [])
                    if not steps:
                        empty_polls += 1
                        t_stat = ls_bridge.get_trajectory(real_conv_id)
                        if t_stat and t_stat.get("status") in ["CASCADE_RUN_STATUS_IDLE", "CASCADE_RUN_STATUS_SUCCESS"]:
                            if step_streamed_lens or empty_polls > 6:
                                is_done = True
                                break
                        continue

                    empty_polls = 0
                    advanced_offset = current_offset

                    for idx_in_batch, step in enumerate(steps):
                        global_idx = current_offset + idx_in_batch
                        stype = step.get("type")
                        status = step.get("status")

                        if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                            pr = step.get("plannerResponse", {})
                            resp_text = pr.get("response") or pr.get("content") or ""
                            thinking_text = pr.get("thinking") or ""

                            prev_len = step_streamed_lens.get(global_idx, 0)
                            if len(resp_text) > prev_len:
                                delta = resp_text[prev_len:]
                                step_streamed_lens[global_idx] = len(resp_text)
                                out_evt = json.dumps({"type": "delta", "content": delta, "thinking": thinking_text})
                                self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
                                self.wfile.flush()

                            if status in ["CORTEX_STEP_STATUS_DONE", "CORTEX_STEP_STATUS_SUCCESS"]:
                                if not pr.get("toolCalls"):
                                    is_done = True
                                advanced_offset = max(advanced_offset, global_idx + 1)

                        elif stype in ["CORTEX_STEP_TYPE_RUN_COMMAND", "CORTEX_STEP_TYPE_TOOL"]:
                            if global_idx not in step_streamed_lens:
                                meta = step.get("metadata", {})
                                action_name = meta.get("toolAction") or step.get("toolName") or "执行工具"
                                summary = meta.get("toolSummary") or "正在执行系统操作..."
                                out_evt = json.dumps({
                                    "type": "delta",
                                    "content": f"\n\n> ⚙️ **[{action_name}]** {summary}\n\n",
                                    "thinking": ""
                                })
                                self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
                                self.wfile.flush()
                                step_streamed_lens[global_idx] = 1

                            if status in ["CORTEX_STEP_STATUS_DONE", "CORTEX_STEP_STATUS_SUCCESS"]:
                                advanced_offset = max(advanced_offset, global_idx + 1)

                        elif stype == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
                            err_obj = step.get("errorMessage", {}).get("error", {})
                            short_err = str(err_obj.get("shortError") or "")
                            user_err = str(err_obj.get("userErrorMessage") or "")

                            # Auto-retry with Flash if capacity exhausted (503) or unrecognized model key
                            if not retried_with_flash and chosen_model != "gemini-3.8-flash" and (
                                "503" in short_err or "capacity" in short_err.lower() or "unknown model" in short_err.lower()
                            ):
                                print(f"[Agy-Web] Model error: {short_err}, auto-retrying with Flash...")
                                retried_with_flash = True
                                chosen_model = "gemini-3.8-flash"
                                ls_bridge.send_user_message(real_conv_id, user_message, model=chosen_model)
                                current_offset = global_idx + 1
                                advanced_offset = current_offset
                                break

                            # Report error directly to UI so user isn't kept waiting
                            err_msg = user_err or short_err or "服务繁忙，请稍后重试"
                            out_evt = json.dumps({
                                "type": "delta",
                                "content": f"\n\n> ⚠️ **[系统提示]** {err_msg}\n\n",
                                "thinking": ""
                            })
                            self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            is_done = True
                            advanced_offset = max(advanced_offset, global_idx + 1)
                            break

                        elif status == "CORTEX_STEP_STATUS_WAITING":
                            traj_step_info = step.get("metadata", {}).get("sourceTrajectoryStepInfo", {})
                            tid = traj_step_info.get("trajectoryId") or real_conv_id
                            s_idx = traj_step_info.get("stepIndex", global_idx)

                            req_inter = step.get("requestedInteraction", {})
                            cmd = ""
                            if "permission" in req_inter:
                                cmd = req_inter["permission"].get("resource", {}).get("target", "")
                            if not cmd:
                                cmd = step.get("generic", {}).get("args", {}).get("CommandLine", "")
                            if not cmd:
                                tool_call = step.get("metadata", {}).get("toolCall", {})
                                try:
                                    args = json.loads(tool_call.get("argumentsJson", "{}"))
                                    cmd = args.get("CommandLine", "")
                                except Exception:
                                    pass

                            desc = req_inter.get("permission", {}).get("actionDescription") or \
                                   step.get("generic", {}).get("args", {}).get("toolSummary") or \
                                   step.get("generic", {}).get("args", {}).get("toolAction") or \
                                   "执行系统命令"

                            if auto_approve:
                                if global_idx not in step_streamed_lens:
                                    step_streamed_lens[global_idx] = 1
                                    out_evt = json.dumps({
                                        "type": "delta",
                                        "content": f"\n\n> ⚡ **[自动批准执行]** `{cmd}`\n\n",
                                        "thinking": ""
                                    })
                                    self.wfile.write(f"data: {out_evt}\n\n".encode("utf-8"))
                                    self.wfile.flush()
                                    ls_bridge.handle_user_interaction(real_conv_id, tid, s_idx, allow=True)
                            else:
                                if global_idx not in step_streamed_lens:
                                    step_streamed_lens[global_idx] = 1
                                    perm_evt = json.dumps({
                                        "type": "permission_request",
                                        "cascade_id": real_conv_id,
                                        "trajectory_id": tid,
                                        "step_index": s_idx,
                                        "command": cmd or "系统操作",
                                        "description": desc
                                    })
                                    self.wfile.write(f"data: {perm_evt}\n\n".encode("utf-8"))
                                    self.wfile.flush()

                            # Do not advance past this waiting step; reset empty_polls
                            empty_polls = 0
                            continue

                        elif stype in ["CORTEX_STEP_TYPE_GENERIC", "CORTEX_STEP_TYPE_RUN_COMMAND"] and status == "CORTEX_STEP_STATUS_ERROR":
                            advanced_offset = max(advanced_offset, global_idx + 1)

                        elif status in ["CORTEX_STEP_STATUS_DONE", "CORTEX_STEP_STATUS_SUCCESS"]:
                            advanced_offset = max(advanced_offset, global_idx + 1)

                    current_offset = advanced_offset

                # Ensure workspace URI is updated in DB
                db_path = Path(user_profile.get("summaries_db", str(Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")))
                if real_conv_id and db_path.exists():
                    try:
                        conn = get_summaries_db(user_profile)
                        if conn:
                            with conn:
                                uri = f"file://{work_dir}"
                                conn.execute("UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?",
                                             (json.dumps([uri]), real_conv_id))
                            conn.close()
                    except Exception:
                        pass

                done_evt = json.dumps({"type": "done", "conversation_id": real_conv_id})
                self.wfile.write(f"data: {done_evt}\n\n".encode("utf-8"))
                self.wfile.flush()
                return

            except Exception as e:
                print(f"[Agy-Web] Error in Live RPC stream: {e}, falling back to CLI...")
                # Fallback to Mode 2 below if exception occurs

        # ======================================================================
        # MODE 2: Offline Fallback CLI Pipeline Mode
        # ======================================================================
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

            db_path = Path(user_profile.get("summaries_db", str(Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db")))
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
    print(f"🚀 [Agy-Web] Starting Dual-Mode Remote Synced Web UI on http://{HOST}:{PORT}")
    cfg = load_auth_config()
    configured_users = list(cfg.get("users", {}).keys())
    print(f"👥 [Agy-Web] Configured isolated users: {configured_users}")

    # Initial Bridge Probe
    inst = ls_bridge.get_instance()
    if inst:
        print(f"🟢 [Agy-Web] Language Server Bridge connected! Port: {inst['port']}, Source: {inst['source']}")
        print(f"✨ [Agy-Web] Full bidirectional synchronization with Google Antigravity Remote is ACTIVE.")
    else:
        print(f"🟡 [Agy-Web] Language Server daemon not found. Running in standalone CLI fallback mode.")

    server = ThreadingHTTPServer((HOST, PORT), AgyMultiUserHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 [Agy-Web] Shutting down.")
        server.server_close()

if __name__ == "__main__":
    main()
