"""Deterministic, non-generative image preparation."""
from pathlib import Path
import math

from PIL import Image, ImageOps

CANVAS = (1080, 1350)
SIZE_WIDTHS = {"小": 0.40, "小中": 0.525, "中": 0.65, "中大": 0.775, "大": 0.90}
BASELINE = 0.875  # bottom of the visible product: 1/8 canvas height above bottom
LAYOUT_SIGNATURE = "alpha-bottom-v2:" + repr((SIZE_WIDTHS, BASELINE, CANVAS))
Image.MAX_IMAGE_PIXELS = 40_000_000


def read_image(source):
    with Image.open(source) as image:
        if image.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("请上传 JPG、PNG 或 WebP 图片。")
        if image.width * image.height > 40_000_000:
            raise ValueError("图片超过 4000 万像素，请先缩小后上传。")
        image.load()
        result = ImageOps.exif_transpose(image).convert("RGBA")
    if min(result.size) < 16:
        raise ValueError("图片太小，请上传至少 16×16 的图片。")
    return result


def normalize_template(image):
    # Flatten alpha onto white before the cover crop; never stretch the image.
    background = Image.new("RGBA", image.size, "white")
    background.alpha_composite(image)
    return ImageOps.fit(background.convert("RGB"), CANVAS, Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5))


def validate_alignment(alignment):
    if not isinstance(alignment, dict) or set(alignment) != {"cx", "cy", "width", "angle"}:
        raise ValueError("摆放参数需要位置、宽度与旋转角度。")
    limits = {"cx": (0, 1), "cy": (0, 1), "width": (0.05, 1), "angle": (-180, 180)}
    result = {}
    for key, (low, high) in limits.items():
        value = alignment[key]
        if type(value) not in {float, int} or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("摆放参数超出范围，请在预览中调整。")
        result[key] = float(value)
    return result


def compose(background, cutout, size, alignment=None):
    alpha = cutout.getchannel("A")
    # Include EVERY nontransparent pixel, including soft/near-transparent edges.
    box = alpha.getbbox()
    if not box:
        raise ValueError("没有检测到可见商品，请重新去背景或更换原图。")
    product = cutout.crop(box)
    width, height = CANVAS
    bottom = height - round(height * (1 - BASELINE))
    if alignment is not None:
        alignment = validate_alignment(alignment)
        scale = width * alignment["width"] / product.width
    else:
        scale = min(width * SIZE_WIDTHS[size] / product.width, bottom / product.height)
    target = (max(1, round(product.width * scale)), max(1, round(product.height * scale)))
    if alignment and max(target) > math.hypot(*CANVAS) + 4:
        raise ValueError("包包超出画面，请先缩小宽度。")
    product = product.resize(target, Image.Resampling.LANCZOS)
    # Resampling can create a fully transparent final row. Trim again so the
    # actual last nontransparent pixel, rather than a padded box, sets the bottom.
    resized_box = product.getchannel("A").getbbox()
    if not resized_box:
        raise ValueError("缩放后没有可见商品，请更换原图。")
    product = product.crop(resized_box)
    rotation = alignment["angle"] if alignment else 0
    if rotation:
        product = product.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
    rotated_box = product.getchannel("A").getbbox()
    product = product.crop(rotated_box)
    position = (round(width * alignment["cx"] - product.width / 2),
                round(height * alignment["cy"] - product.height / 2)) if alignment else (
                (width - product.width) // 2, bottom - product.height)
    if alignment and (position[0] < 0 or position[1] < 0 or position[0] + product.width > width
                      or position[1] + product.height > height):
        raise ValueError("包包超出画面，请缩小或移动回背景内。")
    canvas = background.convert("RGBA")
    canvas.alpha_composite(product, position)
    return canvas.convert("RGB"), {"x": position[0], "y": position[1],
                                     "width": product.width, "height": product.height, "size": size,
                                     "source_box": list(box), "resize_to": list(target),
                                     "resized_box": list(resized_box), "bottom_gap": height - position[1] - product.height,
                                     "angle": rotation, "rotated_box": list(rotated_box),
                                     "canvas": list(CANVAS), "layout": LAYOUT_SIGNATURE}


def product_layer(cutout, placement):
    """Reconstruct the exact foreground used in a composition, including alpha."""
    box = placement.get("source_box")
    if box is None:
        # Historical previews used this cutoff. Restore those at THEIR original
        # crop/size/position rather than moving them to the new layout.
        box = cutout.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if not box:
        raise ValueError("找不到用于还原的商品像素。")
    product = cutout.crop(box).resize(tuple(placement.get("resize_to", (placement["width"], placement["height"]))),
                                     Image.Resampling.LANCZOS)
    if placement.get("resized_box"):
        product = product.crop(placement["resized_box"])
    if placement.get("angle"):
        product = product.rotate(-placement["angle"], resample=Image.Resampling.BICUBIC, expand=True)
    if placement.get("rotated_box"):
        product = product.crop(placement["rotated_box"])
    layer = Image.new("RGBA", tuple(placement.get("canvas", CANVAS)))
    layer.alpha_composite(product, (placement["x"], placement["y"]))
    return layer


def restore_product(generated, layer):
    # Match the entire generated frame to the input canvas, without recropping
    # or independently rescaling the original foreground. API size stays auto.
    background = generated.convert("RGBA")
    if background.size != layer.size:
        background = background.resize(layer.size, Image.Resampling.LANCZOS)
    background.alpha_composite(layer)
    return background


def save_image(image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    image.save(temporary, format="PNG")
    temporary.replace(path)
    thumb = image.copy()
    thumb.thumbnail((360, 450), Image.Resampling.LANCZOS)
    thumb.save(path.with_name(path.stem + "_thumb.png"), format="PNG")
