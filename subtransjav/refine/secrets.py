"""
Refine 密钥存取：DPAPI 加密存储（与旧项目 api_keys.bin 格式互通）
查找顺序（取第一个存在的文件）：
  1. 环境变量 REFINE_SECRETS_FILE 指定的文件
  2. 项目配置目录  <项目根>/config/api_keys.bin（当前标准位置）
  3. 包目录兼容回退 subtransjav/refine/api_keys.bin（历史位置）
  全部不存在时，新建于标准位置（3）。
"""

import base64
import contextlib
import ctypes
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
# 标准位置：项目根/config/（程序与数据分离，包目录保持纯代码）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_CONFIG_STORE = os.path.join(_PROJECT_ROOT, "config", "api_keys.bin")
_PACKAGE_STORE = os.path.join(_HERE, "api_keys.bin")


def get_store_path() -> str:
    env = os.environ.get("REFINE_SECRETS_FILE", "").strip()
    if env:
        return env
    for candidate in (_CONFIG_STORE, _PACKAGE_STORE):
        if os.path.isfile(candidate):
            return candidate
    return _CONFIG_STORE


# ---------------- Windows DPAPI ----------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        0x01, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        0x01, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData 调用失败（密钥库损坏或非本用户加密）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ---------------- 存取接口 ----------------
def _atomic_write(path: str, data: bytes):
    """原子写：临时文件 + fsync + os.replace，避免中途崩溃损坏密钥库。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".api_keys", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def store_secret(name: str, value: str, store_path: str = None):
    """加密保存；value 为空串表示删除该项。

    密钥库损坏时会抛出异常（拒绝覆盖），避免静默清空全部密钥。
    """
    path = store_path or get_store_path()
    secrets = _read_raw(path)
    if value:
        raw_value = value.encode("utf-8")
        if os.name == "nt":
            raw_value = _dpapi_protect(raw_value)
        secrets[name] = base64.b64encode(raw_value.hex().encode()).decode()
    else:
        secrets.pop(name, None)
    payload = json.dumps(secrets).encode("utf-8")
    if os.name == "nt":
        payload = _dpapi_protect(payload)
    _atomic_write(path, payload)


def read_secret(name: str, store_path: str = None) -> str:
    path = store_path or get_store_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as _f:
            raw = _f.read()
        if os.name == "nt":
            raw = _dpapi_unprotect(raw)
        secrets = json.loads(raw.decode("utf-8"))
        enc = secrets.get(name)
        if not enc:
            return ""
        data = bytes.fromhex(base64.b64decode(enc).decode())
        if os.name == "nt":
            data = _dpapi_unprotect(data)
        return data.decode("utf-8")
    except Exception as e:
        # 密钥库损坏/无法解密时不再静默返回空串 —— 记录日志便于排查。
        # 注意：「文件不存在」与「键不存在」已在前面的提前返回中处理，
        # 此处的异常仅代表真实的损坏/解密失败。
        logger.warning("读取密钥 '%s' 失败（密钥库可能损坏或非本用户加密）: %s", name, e)
        return ""


def _read_raw(path: str) -> dict:
    """读取密钥库原始字典。

    文件不存在返回空；文件存在但损坏/无法解密时抛出异常（而非静默返回空），
    从而阻止 store_secret 用空字典覆盖、销毁全部密钥。
    """
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as _f:
        raw = _f.read()
    if os.name == "nt":
        raw = _dpapi_unprotect(raw)
    return json.loads(raw.decode("utf-8"))
