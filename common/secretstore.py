# -*- coding: utf-8 -*-
"""密钥保护层（Windows DPAPI）

问题背景：`config/config.json` 明文保存 AI Key / 飞书 App Secret / 内部
`secret_key`。用户机器上任何进程（或误上传的配置备份）都能直接读走。

方案：Windows 上用 DPAPI（`CryptProtectData`，当前用户作用域）加密敏感字段，
密文以 `enc:v1:<base64>` 形式存回 config.json。DPAPI 密钥由 Windows 账户绑定，
换用户/换机器无法解密——这正是我们要的（凭据不随文件外泄）。

**设计红线（务必保持）**：加解密是"尽力而为"的增强，绝不能因为它让用户丢配置。
- 加密失败 → 原样写明文，只打日志，不抛异常；
- 解密失败（换机/换用户/数据损坏）→ 返回空串并告警，提示重新配置，不崩溃；
- 不带 `enc:v1:` 前缀的值一律视为明文直接使用（向后兼容老配置）。

非 Windows 平台（开发/CI）无 DPAPI，直接透传明文，行为等价于改造前。
"""
from __future__ import annotations

import base64
import logging
import sys
from typing import Any, Dict

log = logging.getLogger("secretstore")

# 密文前缀：带此前缀的字符串才尝试解密，否则视为明文（向后兼容）
_PREFIX = "enc:v1:"

# config.json 中需要保护的字段路径（点号分隔）
SECRET_PATHS = (
    "secret_key",
    "feishu.app_secret",
    "ai.deepseek_api_key",
    "ai.gemini_api_key",
    "ai.qwen_api_key",
)

_IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# DPAPI 绑定（仅 Windows）
# ---------------------------------------------------------------------------
def _dpapi_call(data: bytes, encrypt: bool) -> bytes | None:
    """调用 CryptProtectData / CryptUnprotectData。失败返回 None（不抛）。"""
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()

        fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
        # 参数：pDataIn, szDataDescr, pOptionalEntropy, pvReserved,
        #       pPromptStruct, dwFlags, pDataOut
        # dwFlags=0 → 当前用户作用域（CRYPTPROTECT_UI_FORBIDDEN 无需，非交互）
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
                ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception as e:  # pragma: no cover - 平台/权限异常
        log.debug("DPAPI 调用异常（%s）：%s", "加密" if encrypt else "解密", e)
        return None


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


_available: bool | None = None


def available() -> bool:
    """当前环境是否真的能加密（Windows + DPAPI 可调用）。

    不是简单看 `sys.platform`：Wine、被策略禁用 crypt32、精简版系统都可能
    平台是 win32 却调不通 DPAPI。这里做一次真实的加解密探测并缓存结果，
    供健康检查上报「密钥是否受保护」和测试跳过断言使用。
    """
    global _available
    if _available is None:
        probe = _dpapi_call(b"probe", encrypt=True)
        _available = probe is not None and _dpapi_call(probe, encrypt=False) == b"probe"
        if _IS_WINDOWS and not _available:
            log.warning("DPAPI 不可用，密钥将以明文存储")
    return _available


def protect(plain: str) -> str:
    """加密明文。非 Windows / 空值 / 失败时原样返回明文（绝不丢数据）。"""
    if not plain or not isinstance(plain, str):
        return plain
    if is_encrypted(plain):
        return plain  # 已加密，幂等
    if not _IS_WINDOWS:
        return plain
    blob = _dpapi_call(plain.encode("utf-8"), encrypt=True)
    if blob is None:
        log.warning("密钥加密失败，回退明文存储（功能不受影响）")
        return plain
    return _PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect(token: str) -> str:
    """解密密文。非密文原样返回；解密失败返回空串并告警（提示重配，不崩）。"""
    if not is_encrypted(token):
        return token
    raw = token[len(_PREFIX):]
    try:
        blob = base64.b64decode(raw.encode("ascii"))
    except Exception:
        log.warning("密钥密文格式损坏，已忽略该项，请在管理面板重新配置")
        return ""
    plain = _dpapi_call(blob, encrypt=False)
    if plain is None:
        log.warning(
            "密钥解密失败（配置可能来自其他 Windows 账户或机器）。"
            "该项已置空，请在管理面板重新配置。"
        )
        return ""
    try:
        return plain.decode("utf-8")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 嵌套字典按路径读写
# ---------------------------------------------------------------------------
def _get_path(cfg: Dict[str, Any], path: str):
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(cfg: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def decrypt_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """就地把 cfg 中所有密文字段解密成明文（供运行时注入环境变量）。"""
    for path in SECRET_PATHS:
        val = _get_path(cfg, path)
        if is_encrypted(val):
            _set_path(cfg, path, unprotect(val))
    return cfg


def encrypt_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """返回一份「敏感字段已加密」的深拷贝（用于写盘，不影响内存中的明文）。"""
    import copy
    out = copy.deepcopy(cfg)
    for path in SECRET_PATHS:
        val = _get_path(out, path)
        if isinstance(val, str) and val and not is_encrypted(val):
            _set_path(out, path, protect(val))
    return out


def has_plaintext_secret(cfg: Dict[str, Any]) -> bool:
    """判断配置里是否还存在未加密的敏感字段（用于触发一次性迁移）。"""
    if not _IS_WINDOWS:
        return False
    for path in SECRET_PATHS:
        val = _get_path(cfg, path)
        if isinstance(val, str) and val and not is_encrypted(val):
            return True
    return False
