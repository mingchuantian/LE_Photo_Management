"""Poof's documented multipart removal API, returning full-resolution RGBA PNG."""
import io
import os
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from imaging import read_image

ENDPOINT = "https://api.poof.bg/v1/remove"
MAX_UPLOAD_BYTES = 20_000_000  # Stay within both decimal and binary 20 MB limits.


def load_poof_key(root: Path):
    key = os.getenv("POOF_API_KEY", "").strip()
    if not key:
        path = root / "poof.bg_key"
        if path.exists():
            key = path.read_text(encoding="utf-8-sig").strip()
            if key.startswith("POOF_API_KEY="):
                key = key.split("=", 1)[1].strip().strip("\"'")
    return key


def prepare_upload(image):
    # Bound dimensions and the documented 36 MP maximum without stretching.
    if max(image.size) > 6000:
        image = image.copy()
        image.thumbnail((6000, 6000), Image.Resampling.LANCZOS)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    if stream.tell() <= MAX_UPLOAD_BYTES:
        return "product.png", stream.getvalue(), "image/png"
    # Photo PNGs can be much larger than the original JPEG. Try lossless WebP
    # before asking for a smaller source; do not introduce JPEG artefacts.
    stream = io.BytesIO()
    image.save(stream, "WEBP", lossless=True, exact=True)
    if stream.tell() <= MAX_UPLOAD_BYTES:
        return "product.webp", stream.getvalue(), "image/webp"
    raise ValueError("无损转换后的图片超过 Poof 20 MB 限制，请先缩小原图后重试。")


def remove_background(image, root):
    key = load_poof_key(root)
    if not key:
        raise ValueError("找不到 Poof key，请放入根目录 poof.bg_key。")
    upload = prepare_upload(image)
    # No automatic retries or vendor fallback: a timed-out request may be billed.
    try:
        with httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
            response = client.post(ENDPOINT, headers={"x-api-key": key},
                                   files={"image_file": upload},
                                   data={"format": "png", "channels": "rgba", "size": "full", "crop": "false"})
    except httpx.TimeoutException:
        raise ValueError("Poof 请求超时，可能已计费，请检查后手动重试。") from None
    except httpx.HTTPError:
        raise ValueError("无法连接 Poof，请检查网络后手动重试。") from None
    if response.status_code != 200:
        reasons = {400: "图片或参数不符合要求", 401: "key 无效", 402: "额度不足",
                   403: "当前方案无权调用此接口", 413: "图片超过 20 MB", 422: "无法读取图片",
                   429: "请求过于频繁", 500: "图片处理失败", 502: "处理服务暂不可用",
                   503: "处理服务暂不可用", 504: "处理服务超时"}
        reason = reasons.get(response.status_code, "处理失败")
        try:
            if response.json().get("code") == "image_too_large":
                reason = "图片超过 20 MB"
        except (ValueError, AttributeError):
            pass
        # Never display arbitrary upstream messages, which may echo credentials.
        raise ValueError(f"Poof {reason}（HTTP {response.status_code}），请检查后手动重试。")
    try:
        result = read_image(io.BytesIO(response.content))
    except (ValueError, UnidentifiedImageError, OSError):
        raise ValueError("Poof 返回的内容不是有效图片，请检查后手动重试。") from None
    alpha = result.getchannel("A")
    if alpha.getextrema()[0] == 255:
        raise ValueError("Poof 未返回透明背景，请检查原图后重试。")
    if not alpha.point(lambda a: 255 if a > 8 else 0).getbbox():
        raise ValueError("Poof 未检测到可见商品，请更换原图。")
    return result
