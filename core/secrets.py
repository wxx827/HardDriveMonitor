"""API Key 的本地加密（Windows DPAPI，绑定当前用户）。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x01

__all__ = ["protect", "unprotect", "available"]


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _crypt32():
    return ctypes.windll.crypt32


def _kernel32():
    return ctypes.windll.kernel32


def available() -> bool:
    try:
        return bool(_crypt32())
    except (AttributeError, OSError):
        return False


def protect(text: str) -> str:
    """加密成 base64 字符串；失败抛异常，由调用方降级处理。"""
    if not text:
        return ""
    data = text.encode("utf-8")
    in_blob, _keep = _make_blob(data)
    out_blob = _DATA_BLOB()
    ok = _crypt32().CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        cipher = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32().LocalFree(out_blob.pbData)
    return base64.b64encode(cipher).decode("ascii")


def unprotect(token: str) -> str:
    if not token:
        return ""
    cipher = base64.b64decode(token)
    in_blob, _keep = _make_blob(cipher)
    out_blob = _DATA_BLOB()
    ok = _crypt32().CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    try:
        data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32().LocalFree(out_blob.pbData)
    return data.decode("utf-8", "replace")
