"""Photoroom's dedicated background removal endpoint; no generative edits."""
import io
import os
from pathlib import Path

import httpx
from PIL import Image

from imaging import read_image

ENDPOINT = "https://sdk.photoroom.com/v1/segment"


def clean_photoroom_cutout(image):
    # Real responses can contain alpha=1 across otherwise empty areas. Retain
    # ALL alpha values around the subject, including its soft edges, but clear
    # isolated alpha=1 haze outside that boundary. RGB product pixels never change.
    alpha = image.getchannel("A")
    box = alpha.point(lambda value: 255 if value > 1 else 0).getbbox()
    if not box:
        raise ValueError("Photoroom 未检测到可见商品，请更换原图。")
    left, top, right, bottom = box
    box = (max(0, left - 2), max(0, top - 2), min(image.width, right + 2), min(image.height, bottom + 2))
    clean_alpha = Image.new("L", image.size)
    clean_alpha.paste(alpha.crop(box), box[:2])
    result = image.copy()
    result.putalpha(clean_alpha)
    return result


def load_photoroom_key(root: Path):
    key = os.getenv("PHOTOROOM_API_KEY", "").strip()
    if not key:
        path = root / "photoroom_key"
        if path.exists():
            key = path.read_text(encoding="utf-8-sig").strip()
            if key.startswith("PHOTOROOM_API_KEY="):
                key = key.split("=", 1)[1].strip().strip("\"'")
    return key


def remove_background(image, root):
    key = load_photoroom_key(root)
    if not key:
        raise ValueError("找不到 Photoroom key，请放入根目录 photoroom_key。")
    if max(image.size) > 6000:
        image = image.copy()
        image.thumbnail((6000, 6000), Image.Resampling.LANCZOS)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    if stream.tell() > 50 * 1024 * 1024:
        raise ValueError("转换后的图片超过 Photoroom 50 MB 限制，请先缩小原图。")
    # No automatic retries: a timed-out request may already have been billed.
    with httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
        response = client.post(ENDPOINT, headers={"x-api-key": key},
                               files={"image_file": ("product.png", stream.getvalue(), "image/png")},
                               data={"format": "png", "channels": "rgba", "size": "full",
                                     "crop": "false", "despill": "false"})
    if response.status_code != 200:
        reason = {401: "key 无效", 402: "额度不足", 403: "无权调用此接口",
                  413: "图片太大", 429: "请求过于频繁"}.get(response.status_code, "处理失败")
        raise ValueError(f"Photoroom {reason}（HTTP {response.status_code}），请检查后手动重试。")
    result = read_image(io.BytesIO(response.content))
    if result.getchannel("A").getextrema()[0] == 255:
        raise ValueError("Photoroom 未返回透明背景，请检查原图后重试。")
    return clean_photoroom_cutout(result)
