import io

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

import poof_service


def transparent_png():
    image = Image.new("RGBA", (400, 500))
    ImageDraw.Draw(image).rectangle((50, 100, 350, 350), fill=(100, 70, 40, 255))
    image.putpixel((49, 100), (100, 70, 40, 1))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def mock_api(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(poof_service.httpx, "Client",
                        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))


def test_key_file_bom_assignment_and_environment_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("POOF_API_KEY", raising=False)
    assert poof_service.load_poof_key(tmp_path) == ""
    path = tmp_path / "poof.bg_key"
    path.write_text('POOF_API_KEY="test-file-key"\n', encoding="utf-8-sig")
    assert poof_service.load_poof_key(tmp_path) == "test-file-key"
    monkeypatch.setenv("POOF_API_KEY", " test-env-key ")
    assert poof_service.load_poof_key(tmp_path) == "test-env-key"


def test_real_multipart_contract_and_preserved_transparency(monkeypatch, tmp_path):
    monkeypatch.delenv("POOF_API_KEY", raising=False)
    (tmp_path / "poof.bg_key").write_text("test-poof-key")
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://api.poof.bg/v1/remove"
        assert request.headers["x-api-key"] == "test-poof-key"
        body = request.content.decode("utf-8", errors="replace")
        for name, value in {"format": "png", "channels": "rgba", "size": "full", "crop": "false"}.items():
            assert f'name="{name}"\r\n\r\n{value}\r\n' in body
        assert 'name="image_file"; filename="product.png"' in body
        assert 'name="bg_color"' not in body and 'name="despill"' not in body
        return httpx.Response(200, content=transparent_png(), headers={"Content-Type": "image/png"})
    mock_api(monkeypatch, handler)
    result = poof_service.remove_background(Image.new("RGB", (400, 500)), tmp_path)
    assert len(requests) == 1
    assert result.mode == "RGBA" and result.size == (400, 500)
    assert result.getpixel((49, 100)) == (100, 70, 40, 1)
    assert result.getchannel("A").getextrema() == (0, 255)


@pytest.mark.parametrize("status,code,reason", [
    (401, "authentication_error", "key 无效"), (402, "payment_required", "额度不足"),
    (403, "permission_denied", "当前方案"), (429, "rate_limit_exceeded", "请求过于频繁"),
    (400, "image_too_large", "20 MB"), (422, "invalid_image", "无法读取图片"),
    (502, "upstream_error", "暂不可用")])
def test_errors_never_retry_fallback_or_echo_secrets(monkeypatch, tmp_path, status, code, reason):
    monkeypatch.setenv("POOF_API_KEY", "test-secret-key")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"code": code, "message": "test-secret-key"})
    mock_api(monkeypatch, handler)
    with pytest.raises(ValueError, match=reason) as error:
        poof_service.remove_background(Image.new("RGB", (400, 500)), tmp_path)
    assert len(requests) == 1 and str(requests[0].url) == poof_service.ENDPOINT
    assert "test-secret-key" not in str(error.value)


def test_timeout_never_repeats_request(monkeypatch, tmp_path):
    monkeypatch.setenv("POOF_API_KEY", "test-key")
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("test-key", request=request)
    mock_api(monkeypatch, handler)
    with pytest.raises(ValueError, match="请求超时") as error:
        poof_service.remove_background(Image.new("RGB", (400, 500)), tmp_path)
    assert len(requests) == 1 and "test-key" not in str(error.value)


@pytest.mark.parametrize("kind,reason", [("opaque", "未返回透明背景"), ("empty", "未检测到可见商品"), ("invalid", "不是有效图片")])
def test_invalid_outputs_do_not_enter_pipeline(monkeypatch, tmp_path, kind, reason):
    monkeypatch.setenv("POOF_API_KEY", "test-key")
    stream = io.BytesIO()
    Image.new("RGBA", (400, 500), "white" if kind == "opaque" else (0, 0, 0, 0)).save(stream, "PNG")
    mock_api(monkeypatch, lambda request: httpx.Response(200, content=b"invalid" if kind == "invalid" else stream.getvalue()))
    with pytest.raises(ValueError, match=reason):
        poof_service.remove_background(Image.new("RGB", (400, 500)), tmp_path)


def test_oversize_png_uses_lossless_webp_or_stops_before_api(monkeypatch, tmp_path):
    image = Image.open(io.BytesIO(transparent_png())).convert("RGBA")
    png, webp = io.BytesIO(), io.BytesIO()
    image.save(png, "PNG")
    image.save(webp, "WEBP", lossless=True, exact=True)
    assert len(webp.getvalue()) < len(png.getvalue())
    monkeypatch.setattr(poof_service, "MAX_UPLOAD_BYTES", len(webp.getvalue()))
    name, data, mime = poof_service.prepare_upload(image)
    assert name == "product.webp" and mime == "image/webp"
    assert ImageChops.difference(image, Image.open(io.BytesIO(data)).convert("RGBA")).getbbox() is None
    monkeypatch.setattr(poof_service, "MAX_UPLOAD_BYTES", 1)
    monkeypatch.setenv("POOF_API_KEY", "test-key")
    monkeypatch.setattr(poof_service.httpx, "Client", lambda **kwargs: pytest.fail("Oversize image must not call API"))
    with pytest.raises(ValueError, match="20 MB"):
        poof_service.remove_background(image, tmp_path)


def test_large_dimensions_keep_aspect_ratio_and_source_intact():
    source = Image.new("RGB", (6500, 1000), "tan")
    _, data, _ = poof_service.prepare_upload(source)
    result = Image.open(io.BytesIO(data))
    assert result.size == (6000, 923) and source.size == (6500, 1000)
