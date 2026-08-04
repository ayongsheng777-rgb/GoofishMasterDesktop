# -*- coding: utf-8 -*-
"""At-rest encryption helpers (Fernet) for sensitive JSON files.

Key handling (hardened after external review):
- ANY user-supplied key material is always stretched via PBKDF2-HMAC-SHA256
  (100k iterations, fixed app salt). A 44-char weak passphrase is therefore
  never used directly as a Fernet key.
- Legacy migration: files encrypted with the pre-hardening scheme (raw
  44-char key used directly) are transparently decrypted with the legacy key
  and re-encrypted with the derived key on next save.
- Failure policy: a file with the "fernet:" prefix that fails decryption
  raises CryptoError loudly (no silent plaintext fallback). Plain JSON files
  without the prefix are still accepted (one-time migration path).
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 兜底目录必须落在项目内（Docker 遗留的 "/app/data" 在 Windows 会写到盘符根）。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "feishu-agent")
_KEY_FILE = DATA_DIR / ".encryption_key"
_PREFIX = "fernet:"
_SALT = b"ai-goofish-v2-static-salt"  # app-level salt; secrecy comes from the key material
_PBKDF2_ROUNDS = 100_000

_fernet = None
_fernet_legacy = None


class CryptoError(Exception):
    """Decryption failed on an encrypted file — requires human intervention."""


def _load_key_material() -> str:
    material = os.environ.get("GOOFISH_SECRET_KEY", "").strip()
    if material:
        return material
    if _KEY_FILE.exists():
        return _KEY_FILE.read_text().strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    material = base64.urlsafe_b64encode(os.urandom(32)).decode()
    _KEY_FILE.write_text(material)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    logger.info("已生成加密密钥文件 %s（生产环境建议改用 GOOFISH_SECRET_KEY 环境变量）",
                _KEY_FILE)
    return material


def _derive_key(material: str) -> bytes:
    digest = hashlib.pbkdf2_hmac(
        "sha256", material.encode("utf-8"), _SALT, _PBKDF2_ROUNDS, dklen=32)
    return base64.urlsafe_b64encode(digest)


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_derive_key(_load_key_material()))
    return _fernet


def _get_legacy_fernet():
    """Pre-hardening scheme: raw 44-char base64 key used directly (migration only)."""
    global _fernet_legacy
    if _fernet_legacy is None:
        from cryptography.fernet import Fernet
        material = _load_key_material()
        legacy = None
        if len(material) == 44:
            try:
                base64.urlsafe_b64decode(material.encode())
                legacy = material.encode()
            except Exception:
                legacy = None
        if legacy is None:
            # old fallback was sha256 of material
            legacy = base64.urlsafe_b64encode(
                hashlib.sha256(material.encode()).digest())
        _fernet_legacy = Fernet(legacy)
    return _fernet_legacy


def encrypt_json(data: Dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return _PREFIX + _get_fernet().encrypt(payload).decode()


def decrypt_json(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    if not text.startswith(_PREFIX):
        # Plain JSON (pre-encryption migration path)
        try:
            return json.loads(text)
        except Exception:
            return None

    token = text[len(_PREFIX):].encode()
    try:
        raw = _get_fernet().decrypt(token)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        pass

    # Legacy scheme attempt → transparent migration
    try:
        raw = _get_legacy_fernet().decrypt(token)
        data = json.loads(raw.decode("utf-8"))
        logger.warning("检测到旧方案加密文件，已用旧密钥解密；下次保存将自动升级为强化密钥")
        return data
    except Exception:
        pass

    logger.error("加密文件解密失败（密钥不匹配或文件损坏）——拒绝静默回退明文")
    raise CryptoError(
        "无法解密敏感配置文件：GOOFISH_SECRET_KEY 与文件加密密钥不匹配。"
        "请恢复正确密钥，或删除该文件后重新配置。")


def write_json_file(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encrypt_json(data), encoding="utf-8")


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return decrypt_json(path.read_text(encoding="utf-8"))
    except CryptoError:
        raise
    except Exception as e:
        logger.error("读取加密文件失败 %s: %s", path, e)
        return None
