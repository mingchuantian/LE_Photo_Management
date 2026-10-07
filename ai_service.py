"""The user's Playground recipe. Only n varies between requests."""
import base64
import os
from pathlib import Path

PROMPT = """这个图片是由一个背景模板和一个前面的商品（包）拼在一起的。商品被调整放置在了合适的位置。请你在完全不碰包的比例，大小，以及任何细节的情况下，对环境进行细微优化，要求结果的图片能够让商品自然融入于背景模板。

为了达到这个效果，请你对背景模板的光线进行微调以适应前置商品目前所接收到的光线。同时，请你根据新的光线加入这个商品的阴影。要求非常自然，写实，且保持环境的干净

请不要修改环境本身的布局以及内容，请不要在任何程度上修改商品，仅仅针对光线和相应产生的阴影进行最小限度的调整。"""
SETTINGS = {"model": "gpt-image-2.5-sunburst-2026-09-08", "size": "auto",
            "quality": "max", "background": "auto", "moderation": "auto"}


def load_key(root: Path):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        path = root / "openai_key"
        if path.exists():
            key = path.read_text(encoding="utf-8-sig").strip()
            # Also accept the common OPENAI_API_KEY=... file format.
            if key.startswith("OPENAI_API_KEY="):
                key = key.split("=", 1)[1].strip().strip("\"'")
    return key


def edit_images(path, n, root):
    from openai import OpenAI

    key = load_key(root)
    if not key:
        raise ValueError("找不到 OpenAI key，请放入根目录 openai_key。")
    # Avoid automatic repeats of potentially billable requests after a timeout.
    settings = dict(SETTINGS)
    moderation = settings.pop("moderation")
    with OpenAI(api_key=key, timeout=900, max_retries=0) as client, open(path, "rb") as image:
        # Python SDK method is `edit` (singular). extra_body passes the exact
        # moderation setting even on SDK versions that do not expose it yet.
        response = client.images.edit(image=image, prompt=PROMPT, n=n,
                                      **settings, extra_body={"moderation": moderation})
    if not response.data:
        raise ValueError("OpenAI 没有返回图片。")
    return [base64.b64decode(item.b64_json, validate=True) for item in response.data]
