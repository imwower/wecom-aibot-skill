"""图片 / 文件 / 语音的下载与 AES-256-CBC 解密。

企微回调里的 image/file/voice/video 带 url 与 aeskey：
  - url 下载到的是密文，**5 分钟内有效**，必须收到消息就立刻下载；
  - aeskey 是 Base64 编码的 32 字节 AES key，IV 取 key 的前 16 字节；
  - 密文按 PKCS#7 填充，解密后去掉填充。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import ssl
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("wecom.media")

_SSL = ssl.create_default_context()


def decrypt(data: bytes, aes_key: str) -> bytes:
    """AES-256-CBC 解密，IV = key[:16]，手动去 PKCS#7 填充。"""
    if not data:
        raise ValueError("待解密数据为空")
    if not aes_key:
        raise ValueError("aeskey 为空")
    pad = "=" * (-len(aes_key) % 4)
    key = base64.b64decode(aes_key + pad)
    iv = key[:16]
    block = 16
    if len(data) % block:
        data = data + b"\x00" * (block - len(data) % block)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(data) + dec.finalize()
    if not plain:
        raise ValueError("解密结果为空")
    pad_len = plain[-1]
    if 1 <= pad_len <= 32 and pad_len <= len(plain):
        if all(plain[i] == pad_len for i in range(len(plain) - pad_len, len(plain))):
            return plain[: len(plain) - pad_len]
    # 填充不合法时原样返回，交给调用方判断（比手工报错更不容易丢数据）
    log.warning("PKCS#7 填充异常（pad=%s），返回未去填充的数据", pad_len)
    return plain


def _filename_from_headers(cd: str) -> Optional[str]:
    if not cd:
        return None
    m = re.search(r"filename\*=UTF-8''([^;\s]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip())
    return None


def _safe_name(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").strip()
    return name[:120] or "file"


async def download_and_decrypt(
    url: str,
    aes_key: str,
    dest_dir: Path,
    *,
    kind: str = "file",
    msgid: str = "",
    timeout: float = 30.0,
) -> Path:
    """下载并解密，保存到 dest_dir，返回本地路径。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    conn = aiohttp.TCPConnector(ssl=_SSL)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), connector=conn
    ) as s:
        async with s.get(url) as resp:
            resp.raise_for_status()
            blob = await resp.read()
            remote_name = _filename_from_headers(resp.headers.get("Content-Disposition", ""))
            ctype = resp.headers.get("Content-Type", "")

    plain = decrypt(blob, aes_key) if aes_key else blob

    if remote_name:
        base = _safe_name(remote_name)
    else:
        ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or _default_ext(kind)
        base = f"{kind}{ext}"
    stem = f"{int(time.time())}_{_safe_name(msgid)[:24] or 'msg'}"
    out = dest_dir / f"{stem}_{base}"
    out.write_bytes(plain)
    log.info("媒体已保存：%s（%d 字节）", out, len(plain))
    return out


def _default_ext(kind: str) -> str:
    return {"image": ".jpg", "voice": ".amr", "video": ".mp4"}.get(kind, ".bin")
