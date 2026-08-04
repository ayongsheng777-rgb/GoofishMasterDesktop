# -*- coding: utf-8 -*-
"""Auth Module — TOTP (Time-based One-Time Password) authentication for admin panel.

Uses pyotp for Google Authenticator compatible TOTP.
Session tokens are simple HMAC-signed tokens with expiry.
"""
from __future__ import annotations
import hashlib, hmac, json, logging, os, secrets, time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 兜底目录必须落在项目内（Docker 遗留的 "/app/data" 在 Windows 会写到盘符根）。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "feishu-agent")
AUTH_FILE = DATA_DIR / "auth_config.json"

# Session storage: token -> {"created": float, "expires": float}（内存兜底）
_sessions: Dict[str, Dict[str, float]] = {}
SESSION_DURATION = 86400  # 24 hours

# 登录失败限流（防 TOTP 6 位码爆破）
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 300  # 5 分钟内最多 5 次失败
_login_fail_store: Dict[str, List[float]] = {}

# Redis 会话持久化 + 限流（容器重建不登出）。懒加载，失败回退内存。
_redis_client = None


REDIS_ENABLED = os.environ.get("REDIS_ENABLED", "true").lower() in ("1", "true", "yes", "on")

def _redis():
    """返回 Redis 客户端，失败返回 None（调用方须处理兜底）。"""
    global _redis_client
    if _redis_client is None:
        if not REDIS_ENABLED:
            logger.info("Redis 未启用（可选组件），会话/限流回退内存")
            _redis_client = False
            return None
        try:
            # P1 单用户化：用进程内 fakeredis 替代外部 Redis 服务（零依赖）。
            # 其余 API（set/get/ttl/delete/incr/expire/ping + nx）完全兼容。
            import fakeredis
            client = fakeredis.FakeStrictRedis(decode_responses=True)
            client.ping()
            _redis_client = client
        except Exception as e:
            logger.warning("Redis(fakeredis) 不可用，会话/限流回退内存: %s", e)
            _redis_client = False
    return _redis_client if _redis_client else None


# ---------------- Session store (Redis 优先，内存兜底) ----------------

def _save_session(token: str, expires_in: int) -> None:
    r = _redis()
    try:
        if r is not None:
            r.set(f"session:{token}", "1", ex=int(expires_in))
            return
    except Exception as e:
        logger.warning("session 写 Redis 失败，回退内存: %s", e)
    _sessions[token] = {"created": time.time(),
                        "expires": time.time() + expires_in}


def _load_session(token: str):
    r = _redis()
    try:
        if r is not None:
            ttl = r.ttl(f"session:{token}")
            if ttl is None or ttl < 0:
                return None
            return {"expires": time.time() + max(ttl, 0)}
    except Exception:
        pass
    return _sessions.get(token)


def _del_session(token: str) -> None:
    r = _redis()
    try:
        if r is not None:
            r.delete(f"session:{token}")
    except Exception:
        pass
    _sessions.pop(token, None)


# ---------------- 登录限流 ----------------

def is_login_limited(identifier: str) -> bool:
    """是否已达到登录失败上限（True=触发限流）。"""
    r = _redis()
    key = f"login_attempts:{identifier}"
    try:
        if r is not None:
            cnt = int(r.get(key) or 0)
            return cnt >= _LOGIN_MAX_ATTEMPTS
    except Exception:
        pass
    now = time.time()
    lst = [t for t in _login_fail_store.get(identifier, [])
           if now - t < _LOGIN_WINDOW]
    _login_fail_store[identifier] = lst
    return len(lst) >= _LOGIN_MAX_ATTEMPTS


def record_login_failure(identifier: str) -> None:
    r = _redis()
    key = f"login_attempts:{identifier}"
    try:
        if r is not None:
            cnt = r.incr(key)
            if cnt == 1:
                r.expire(key, _LOGIN_WINDOW)
            return
    except Exception:
        pass
    _login_fail_store.setdefault(identifier, []).append(time.time())


def reset_login_failure(identifier: str) -> None:
    r = _redis()
    try:
        if r is not None:
            r.delete(f"login_attempts:{identifier}")
    except Exception:
        pass
    _login_fail_store.pop(identifier, None)


def _get_secret_key() -> str:
    """Get or create the server secret key for signing tokens."""
    key_file = DATA_DIR / ".server_secret"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    return key


def load_auth_config() -> Dict[str, Any]:
    """Load auth configuration (supports encrypted-at-rest file)."""
    if AUTH_FILE.exists():
        try:
            from crypto import read_json_file
            data = read_json_file(AUTH_FILE)
            if data is not None:
                return data
        except Exception:
            pass
        try:
            return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"totp_secret": "", "setup_completed": False, "created_at": None}


def save_auth_config(config: Dict[str, Any]) -> None:
    """Save auth configuration (encrypted at rest when crypto is available)."""
    try:
        from crypto import write_json_file
        write_json_file(AUTH_FILE, config)
        return
    except Exception:
        pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def is_setup_completed() -> bool:
    """Check if TOTP setup has been completed."""
    config = load_auth_config()
    return config.get("setup_completed", False) and bool(config.get("totp_secret"))


def generate_totp_secret() -> str:
    """Generate a new TOTP secret."""
    import pyotp
    return pyotp.random_base32()


def get_totp_uri(secret: str, issuer: str = "AI-Goofish-V2") -> str:
    """Get TOTP provisioning URI for QR code."""
    import pyotp
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name="admin", issuer_name=issuer)


def generate_totp_qrcode(secret: str) -> str:
    """Generate QR code image (base64 PNG) for TOTP setup."""
    import base64, io
    import segno
    uri = get_totp_uri(secret)
    qr = segno.make(uri, error="M")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)
    return base64.b64encode(buf.getvalue()).decode()


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code."""
    import pyotp
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)  # Allow 1 window tolerance


def setup_totp(secret: str, code: str) -> Dict[str, Any]:
    """Complete TOTP setup by verifying the first code."""
    if not secret:
        return {"success": False, "error": "请先生成密钥"}
    if not code:
        return {"success": False, "error": "请输入验证码"}

    if not verify_totp(secret, code):
        return {"success": False, "error": "验证码错误，请重试"}

    config = {
        "totp_secret": secret,
        "setup_completed": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_auth_config(config)
    logger.info("TOTP setup completed")
    return {"success": True, "message": "验证器设置成功"}


def authenticate(code: str) -> Dict[str, Any]:
    """Authenticate with TOTP code. Returns session token on success."""
    config = load_auth_config()
    secret = config.get("totp_secret", "")

    if not secret:
        return {"success": False, "error": "验证器未设置"}

    if not verify_totp(secret, code):
        return {"success": False, "error": "验证码错误"}

    # Create session token
    token = _create_session_token()
    return {"success": True, "token": token, "expires_in": SESSION_DURATION}


def _create_session_token() -> str:
    """Create a signed session token (persisted to Redis)."""
    secret_key = _get_secret_key()
    payload = f"{time.time()}:{secrets.token_hex(16)}"
    signature = hmac.new(
        secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    token = f"{payload}:{signature}"
    _save_session(token, SESSION_DURATION)
    return token


def verify_session(token: str) -> bool:
    """Verify a session token (Redis-backed, Redis TTL enforces expiry)."""
    if not token:
        return False
    session = _load_session(token)
    if not session:
        return False
    if session["expires"] < time.time():
        _del_session(token)
        return False
    return True


def logout(token: str) -> None:
    """Invalidate a session token."""
    _del_session(token)


def get_current_secret() -> str:
    """Get current TOTP secret (for display during setup)."""
    config = load_auth_config()
    return config.get("totp_secret", "")
