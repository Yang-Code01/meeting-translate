"""API key 的本地加密：Windows DPAPI（CryptProtectData），零第三方依赖。

密文绑定当前 Windows 用户，换机器或换用户解不开——这是刻意的：
config.json 被截图、备份或贴进 issue 时不会泄露 key。
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

PREFIX = "dpapi:"


class SecretError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _Blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(text: str) -> str:
    """返回 dpapi: 前缀的 base64 密文；空串原样返回。"""
    if not text:
        return ""
    if sys.platform != "win32":
        raise SecretError("当前平台不支持 DPAPI")
    data_in = _to_blob(text.encode("utf-8"))
    data_out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    )
    if not ok:
        raise SecretError("加密失败（DPAPI 返回错误）")
    return PREFIX + base64.b64encode(_from_blob(data_out)).decode("ascii")


def unprotect(token: str) -> str:
    """解开 dpapi 密文；不带前缀的明文原样返回（兼容手改过的配置）。"""
    if not token or not token.startswith(PREFIX):
        return token
    if sys.platform != "win32":
        raise SecretError("当前平台不支持 DPAPI")
    raw = base64.b64decode(token[len(PREFIX):])
    data_in = _to_blob(raw)
    data_out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    )
    if not ok:
        raise SecretError("解密失败：配置可能来自另一台机器或另一个 Windows 用户")
    return _from_blob(data_out).decode("utf-8")


def looks_encrypted(token: str) -> bool:
    return bool(token) and token.startswith(PREFIX)
