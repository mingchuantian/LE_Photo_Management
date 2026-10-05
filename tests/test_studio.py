import base64
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image, ImageChops, ImageDraw

from ai_service import PROMPT, SETTINGS, edit_images
from app import create_app
from imaging import (CANVAS, SIZE_WIDTHS, compose, normalize_template, product_layer,
                     read_image, restore_product, save_image)


def image_bytes(size=(400, 500), color="white", format="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format=format)
    return stream.getvalue()


def cutout(_image):
    image = Image.new("RGBA", (400, 500))
    ImageDraw.Draw(image).rectangle((50, 100, 350, 350), fill=(100, 70, 40, 255))
    return image


@pytest.fixture
def studio_app(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path / "data", "DISABLE_WORKERS": True,
                      "REMOVER": cutout, "EDITOR": lambda path, n, root: [image_bytes((800, 1000), "tan") for _ in range(n)]})
    yield app
    app.extensions["studio"].local.shutdown()
    app.extensions["studio"].ai.shutdown()
    app.extensions["studio"].composition.shutdown()


def upload(client, kind, count=1, size=(401, 498)):
    return client.post(f"/api/{kind}", data={"files": [(io.BytesIO(image_bytes(size)), f"商品 {i}.png") for i in range(count)]})


def prepared(app, products=1, templates=2):
    client = app.test_client()
    upload(client, "templates", templates)
    response = upload(client, "products", products)
    app.extensions["studio"].run_job(response.json["job_id"])
    if templates:
        for product in client.get("/api/state").json["products"]:
            confirm_default(app, client, product["id"])
    return client, client.get("/api/state").json


def confirm_default(app, client, product_id, alignment=None):
    preview = client.get(f"/api/products/{product_id}/alignment").json
    response = client.post(f"/api/products/{product_id}/confirm-placement", json={
        "revision": preview["revision"], "template_id": preview["template_id"],
        "alignment": alignment or preview["alignment"]})
    assert response.status_code == 200, response.json
    app.extensions["studio"].run_job(response.json["job_id"])


def test_template_cover_crop_no_distortion_and_alpha():
    image = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((0, 100, 399, 499), fill="red")
    result = normalize_template(image)
    assert result.size == CANVAS and result.mode == "RGB"
    assert result.getpixel((540, 675)) == (255, 0, 0)
    assert result.getpixel((540, 0)) == (255, 255, 255)


def test_exif_orientation():
    stream = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (600, 400)).save(stream, "JPEG", exif=exif)
    stream.seek(0)
    assert read_image(stream).size == (400, 600)


def test_five_sizes_trim_transparent_margin_and_keep_baseline():
    widths = []
    for size, proportion in SIZE_WIDTHS.items():
        image, placement = compose(Image.new("RGB", CANVAS), cutout(None), size)
        widths.append(placement["width"])
        assert placement["width"] == round(1080 * proportion)
        assert placement["y"] + placement["height"] == 1350 - round(1350 / 8)
        assert abs(placement["width"] / placement["height"] - 301 / 251) < .005
        assert image.size == CANVAS
    assert sorted(widths) == widths
    tall = Image.new("RGBA", (100, 2000), "red")
    _, position = compose(Image.new("RGB", CANVAS), tall, "大")
    assert position["y"] >= 0
    with pytest.raises(ValueError):
        compose(Image.new("RGB", CANVAS), Image.new("RGBA", (20, 20)), "中")


def test_full_batch_pipeline_size_history_generation_and_zip(studio_app):
    client, state = prepared(studio_app, products=2)
    assert len(state["composites"]) == 4
    assert list(state["sizes"]) == ["小", "小中", "中", "中大", "大"]
    assert all(p["status"] == "ready" for p in state["products"])
    for t in state["templates"]:
        with Image.open(studio_app.extensions["studio"].media / t["file"]) as im:
            assert im.size == CANVAS
    ids = [c["id"] for c in state["composites"]]
    response = client.post("/api/generate", json={"ids": ids, "n": 3})
    assert response.status_code == 202
    job_id = response.json["job_id"]
    assert client.post("/api/generate", json={"ids": ids, "n": 3}).json["job_id"] is None
    studio_app.extensions["studio"].run_job(job_id)
    state = client.get("/api/state").json
    assert len(state["results"]) == 12
    assert all((r["width"], r["height"]) == CANVAS for r in state["results"])
    assert all((r["raw_width"], r["raw_height"]) == (800, 1000) for r in state["results"])
    assert all(r["product_restored"] == 1 for r in state["results"])
    assert state["jobs"][0]["status"] == "done"
    zipped = client.post("/api/download", json={"ids": [r["id"] for r in state["results"][:2]]})
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.data)) as archive:
        assert len(archive.namelist()) == 2
        assert Image.open(io.BytesIO(archive.read(archive.namelist()[0]))).size == CANVAS
    product_id = state["products"][0]["id"]
    response = client.patch("/api/products/size", json={"ids": [product_id], "size": "大"})
    assert response.json["job_id"] is None
    confirm_default(studio_app, client, product_id)
    changed = client.get("/api/state").json
    assert len(changed["composites"]) == 6
    assert len([c for c in changed["composites"] if c["active"]]) == 4
    assert len(changed["results"]) == 12
    stale = next(c for c in changed["composites"] if not c["active"])
    assert client.post("/api/generate", json={"ids": [stale["id"]]}).status_code == 400
    assert client.get(f"/api/download/cutout/{product_id}").status_code == 200


def test_template_added_after_product_and_soft_delete(studio_app):
    client, state = prepared(studio_app, templates=0)
    assert not state["composites"]
    response = upload(client, "templates")
    assert response.json["job_id"] is None
    confirm_default(studio_app, client, state["products"][0]["id"])
    state = client.get("/api/state").json
    assert len(state["composites"]) == 1
    composite_id = state["composites"][0]["id"]
    job = client.post("/api/generate", json={"ids": [composite_id], "n": 1}).json["job_id"]
    studio_app.extensions["studio"].run_job(job)
    assert client.delete(f"/api/templates/{state['templates'][0]['id']}").status_code == 200
    state = client.get("/api/state").json
    assert state["templates"] == [] and len(state["results"]) == 1
    assert state["composites"][0]["active"] == 0
    assert client.post("/api/generate", json={"ids": [composite_id]}).status_code == 400


def test_partial_failures_survive_and_retry_only_failed(studio_app):
    client, state = prepared(studio_app)
    good, bad = [c["id"] for c in state["composites"]]
    bad_file = next(c["file"] for c in state["composites"] if c["id"] == bad)

    def editor(path, n, root):
        if str(path).replace("\\", "/").endswith(bad_file):
            raise RuntimeError("429 Rate limited")
        return [image_bytes() for _ in range(n)]

    studio_app.config["EDITOR"] = editor
    job = client.post("/api/generate", json={"ids": [good, bad], "n": 2}).json["job_id"]
    studio_app.extensions["studio"].run_job(job)
    state = client.get("/api/state").json
    assert state["jobs"][0]["status"] == "partial"
    assert len(state["results"]) == 2
    retried = client.post(f"/api/jobs/{job}/retry", json={}).json["job_id"]
    items = studio_app.extensions["studio"].rows("SELECT ref_id FROM job_items WHERE job_id=?", (retried,))
    assert items == [{"ref_id": bad}]


def test_invalid_inputs_path_traversal_and_origin(studio_app):
    client, state = prepared(studio_app)
    for n in [0, 11, True, "3", 1.5]:
        assert client.post("/api/generate", json={"ids": [state["composites"][0]["id"]], "n": n}).status_code == 400
    assert client.patch("/api/products/size", json={"ids": [state["products"][0]["id"]], "size": "巨大"}).status_code == 400
    assert client.post("/api/products", data={"files": (io.BytesIO(b"not an image"), "fake.png")}).status_code == 400
    assert client.post("/api/products", headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/media/../../openai_key").status_code == 404
    assert client.get("/openai_key").status_code == 404
    assert client.post("/api/generate", json={"ids": "bad"}).status_code == 400
    assert "sk-" not in json.dumps(state)


def test_restart_does_not_reissue_ai_requests(studio_app):
    client, state = prepared(studio_app)
    job = client.post("/api/generate", json={"ids": [state["composites"][0]["id"]]}).json["job_id"]
    second = create_app({"DATA_DIR": studio_app.config["DATA_DIR"], "DISABLE_WORKERS": True})
    try:
        state = second.test_client().get("/api/state").json
        resumed = next(j for j in state["jobs"] if j["id"] == job)
        assert resumed["status"] == "interrupted"
        assert resumed["items"][0]["status"] == "interrupted"
        assert not state["results"]
    finally:
        second.extensions["studio"].local.shutdown()
        second.extensions["studio"].ai.shutdown()
        second.extensions["studio"].composition.shutdown()


def test_openai_recipe_exact_payload(monkeypatch, tmp_path):
    import openai
    import httpx

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    source = tmp_path / "image.png"
    source.write_bytes(image_bytes())
    received = {}

    def handler(request):
        # Exercise the real SDK serializer without transmitting any request.
        received["body"] = request.content
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})

    real_client = openai.OpenAI
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: real_client(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    outputs = edit_images(source, 7, tmp_path)
    body = received["body"].decode("utf-8", errors="replace")
    for key, value in {**SETTINGS, "prompt": PROMPT, "n": 7}.items():
        assert f'name="{key}"\r\n\r\n{value}\r\n' in body
    assert len(outputs) == 1
    assert "name=\"mask\"" not in body


def test_remove_failure_can_retry(studio_app):
    def failure(image):
        raise RuntimeError("model unavailable")
    studio_app.config["REMOVER"] = failure
    client = studio_app.test_client()
    response = upload(client, "products")
    studio_app.extensions["studio"].run_job(response.json["job_id"])
    product = client.get("/api/state").json["products"][0]
    assert product["status"] == "error"
    studio_app.config["REMOVER"] = cutout
    retry = client.post("/api/products/retry", json={"ids": [product["id"]]})
    studio_app.extensions["studio"].run_job(retry.json["job_id"])
    assert client.get("/api/state").json["products"][0]["status"] == "ready"


def test_bottom_uses_last_nontransparent_pixel_even_if_faint():
    foreground = cutout(None)
    foreground.putpixel((200, 430), (100, 70, 40, 1))
    background = Image.new("RGB", CANVAS, "white")
    result, placement = compose(background, foreground, "小")
    layer = product_layer(foreground, placement)
    bottom = layer.getchannel("A").getbbox()[3]
    assert CANVAS[1] - bottom == round(CANVAS[1] / 8)
    assert placement["source_box"][3] == 431
    expected = background.convert("RGBA")
    expected.alpha_composite(layer)
    assert ImageChops.difference(result, expected.convert("RGB")).getbbox() is None
    assert list(SIZE_WIDTHS.values()) == [0.4, 0.525, 0.65, 0.775, 0.9]


@pytest.mark.parametrize("dimensions", [CANVAS, (800, 1000), (1024, 1536)])
def test_original_product_overwrites_ai_pixels_at_original_coordinates(dimensions):
    _, placement = compose(Image.new("RGB", CANVAS, "white"), cutout(None), "大")
    layer = product_layer(cutout(None), placement)
    restored = restore_product(Image.new("RGB", dimensions, "red"), layer)
    center = (placement["x"] + placement["width"] // 2, placement["y"] + placement["height"] // 2)
    assert restored.size == CANVAS
    assert restored.getpixel(center)[:3] == (100, 70, 40)
    assert restored.getpixel((0, 0))[:3] == (255, 0, 0)
    opaque = layer.getchannel("A").point(lambda a: 255 if a == 255 else 0)
    difference = ImageChops.difference(restored.convert("RGB"), layer.convert("RGB"))
    assert Image.composite(difference, Image.new("RGB", CANVAS), opaque).getbbox() is None


def test_generation_uses_frozen_foreground_and_retains_raw_ai(studio_app):
    client, state = prepared(studio_app, templates=1)
    composite = state["composites"][0]
    product = state["products"][0]
    studio = studio_app.extensions["studio"]

    def editor(path, n, root):
        # Simulate another edit while the API request is in flight.
        save_image(Image.new("RGBA", (400, 500), "blue"), studio.media / product["cutout"])
        return [image_bytes((800, 1000), "red")]

    studio_app.config["EDITOR"] = editor
    job = client.post("/api/generate", json={"ids": [composite["id"]], "n": 1}).json["job_id"]
    studio.run_job(job)
    result = client.get("/api/state").json["results"][0]
    placement = json.loads(composite["placement"])
    center = (placement["x"] + placement["width"] // 2, placement["y"] + placement["height"] // 2)
    assert read_image(studio.media / result["file"]).getpixel(center)[:3] == (100, 70, 40)
    assert read_image(studio.media / result["raw_file"]).getpixel((400, 500))[:3] == (255, 0, 0)
    assert client.get(f"/api/download/raw-result/{result['id']}").status_code == 200


def test_layout_migration_refreshes_previews_and_restores_existing_results(studio_app):
    client, state = prepared(studio_app, templates=1)
    studio = studio_app.extensions["studio"]
    composite = state["composites"][0]
    job_id = studio.queue("generate", [composite["id"]], 1)
    raw_file = "results/legacy.png"
    save_image(Image.new("RGB", (800, 1000), "red"), studio.media / raw_file)
    studio.write("INSERT INTO results (id,composite_id,job_id,file,variant,width,height,created) VALUES (?,?,?,?,?,?,?,?)",
                 ("legacy", composite["id"], job_id, raw_file, 1, 800, 1000, "2026-10-05T00:00:00+00:00"))
    studio.write("UPDATE studio_meta SET value='old-layout' WHERE key='layout'")
    second = create_app({"DATA_DIR": studio_app.config["DATA_DIR"], "DISABLE_WORKERS": True})
    try:
        upgraded = second.extensions["studio"]
        for job in upgraded.rows("SELECT id FROM jobs WHERE status='queued' ORDER BY created"):
            assert upgraded.one("SELECT kind FROM jobs WHERE id=?", (job["id"],))["kind"] in {"restore", "compose"}
            upgraded.run_job(job["id"])
        result = upgraded.one("SELECT * FROM results WHERE id='legacy'")
        assert result["product_restored"] == 1 and result["raw_file"] == raw_file
        assert read_image(upgraded.media / raw_file).size == (800, 1000)
        place = json.loads(composite["placement"])
        center = (place["x"] + place["width"] // 2, place["y"] + place["height"] // 2)
        assert read_image(upgraded.media / result["file"]).getpixel(center)[:3] == (100, 70, 40)
        state = second.test_client().get("/api/state").json
        assert len(state["composites"]) == 2
        assert sum(c["active"] for c in state["composites"]) == 1
        assert state["results"][0]["composite_id"] == composite["id"]
    finally:
        second.extensions["studio"].local.shutdown()
        second.extensions["studio"].ai.shutdown()
        second.extensions["studio"].composition.shutdown()


def test_staged_workflow_preview_then_confirm_then_all_templates(studio_app):
    client = studio_app.test_client()
    upload(client, "templates", 2)
    job = upload(client, "products").json["job_id"]
    studio = studio_app.extensions["studio"]
    studio.run_job(job)
    state = client.get("/api/state").json
    product = state["products"][0]
    assert product["status"] == "ready" and product["placement_confirmed"] == 0
    assert state["composites"] == []
    data = client.get(f"/api/products/{product['id']}/alignment").json
    transform = {"cx": .48, "cy": .64, "width": .6, "angle": 17.5}
    body = {"revision": data["revision"], "template_id": data["template_id"], "alignment": transform}
    preview = client.post(f"/api/products/{product['id']}/preview", json=body)
    assert preview.status_code == 200 and preview.mimetype == "image/png"
    assert client.get("/api/state").json["composites"] == []
    saved = client.post(f"/api/products/{product['id']}/confirm-placement", json=body)
    assert saved.status_code == 200
    studio.run_job(saved.json["job_id"])
    state = client.get("/api/state").json
    assert len(state["composites"]) == 2 and all(c["active"] for c in state["composites"])
    current = next(c for c in state["composites"] if c["template_id"] == data["template_id"])
    assert ImageChops.difference(read_image(io.BytesIO(preview.data)), read_image(studio.media / current["file"])).getbbox() is None
    placements = [json.loads(c["placement"]) for c in state["composites"]]
    assert all(p["angle"] == 17.5 for p in placements)
    assert len({(p["x"], p["y"], p["width"], p["height"]) for p in placements}) == 1
    # A stale open editor must not overwrite a newer decision.
    assert client.post(f"/api/products/{product['id']}/confirm-placement", json=body).status_code == 400
    # New templates only compose confirmed products.
    added = upload(client, "templates").json["job_id"]
    studio.run_job(added)
    assert sum(c["active"] for c in client.get("/api/state").json["composites"]) == 3
    # Re-removal invalidates confirmation and leaves snapshots/history intact.
    old_cutout = product["cutout"]
    recut = client.post("/api/products/remove-background", json={"ids": [product["id"]]}).json["job_id"]
    assert not any(c["active"] for c in client.get("/api/state").json["composites"])
    studio.run_job(recut)
    product = client.get("/api/state").json["products"][0]
    assert product["cutout"] != old_cutout and product["placement_confirmed"] == 0
    assert (studio.media / old_cutout).exists()


def test_rotation_restore_is_identical_and_preview_rejects_clipping(studio_app):
    background = Image.new("RGB", CANVAS, "white")
    transform = {"cx": .5, "cy": .65, "width": .6, "angle": -23}
    composite, placement = compose(background, cutout(None), "中", transform)
    layer = product_layer(cutout(None), placement)
    reconstructed = restore_product(background, layer).convert("RGB")
    assert ImageChops.difference(composite, reconstructed).getbbox() is None
    protected = restore_product(Image.new("RGB", (800, 1000), "red"), layer)
    opaque = layer.getchannel("A").point(lambda a: 255 if a == 255 else 0)
    difference = ImageChops.difference(protected.convert("RGB"), layer.convert("RGB"))
    assert Image.composite(difference, Image.new("RGB", CANVAS), opaque).getbbox() is None
    client, state = prepared(studio_app, templates=1)
    product = state["products"][0]
    data = client.get(f"/api/products/{product['id']}/alignment").json
    for alignment in [{**transform, "cx": 0}, {**transform, "width": 2}, {**transform, "angle": True},
                      {**transform, "cy": float("nan")}, {"width": .5}]:
        assert client.post(f"/api/products/{product['id']}/preview", json={
            "revision": data["revision"], "template_id": data["template_id"], "alignment": alignment}).status_code == 400


def test_photoroom_real_http_payload_without_network(monkeypatch, tmp_path):
    import httpx
    import photoroom_service
    key = "private-photoroom-test"
    (tmp_path / "photoroom_key").write_text("PHOTOROOM_API_KEY=" + key)
    monkeypatch.delenv("PHOTOROOM_API_KEY", raising=False)
    received = {}
    def handler(request):
        received["request"] = request
        stream = io.BytesIO(); cutout(None).save(stream, "PNG")
        return httpx.Response(200, content=stream.getvalue(), headers={"content-type": "image/png"})
    real_client = httpx.Client
    monkeypatch.setattr(photoroom_service.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    image = photoroom_service.remove_background(Image.new("RGB", (400, 500), "white"), tmp_path)
    request = received["request"]
    assert str(request.url) == photoroom_service.ENDPOINT and request.method == "POST"
    assert request.headers["x-api-key"] == key
    body = request.content.decode("utf-8", errors="replace")
    for name, value in {"format": "png", "channels": "rgba", "size": "full", "crop": "false", "despill": "false"}.items():
        assert f'name="{name}"\r\n\r\n{value}\r\n' in body
    assert 'name="image_file"; filename="product.png"' in body
    assert image.mode == "RGBA" and image.getchannel("A").getextrema() == (0, 255)


def test_photoroom_failures_do_not_retry_or_expose_key(monkeypatch, tmp_path):
    import httpx
    import photoroom_service
    monkeypatch.setenv("PHOTOROOM_API_KEY", "secret-photo-key")
    count = []
    def handler(request):
        count.append(request)
        return httpx.Response(429, json={"error": "secret-photo-key"})
    real_client = httpx.Client
    monkeypatch.setattr(photoroom_service.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="429") as error:
        photoroom_service.remove_background(Image.new("RGB", (400, 500)), tmp_path)
    assert len(count) == 1 and "secret-photo-key" not in str(error.value)


def test_photoroom_alpha_one_haze_does_not_shrink_the_actual_product():
    from photoroom_service import clean_photoroom_cutout
    image = Image.new("RGBA", (400, 500), (200, 190, 180, 1))
    d = ImageDraw.Draw(image)
    d.rectangle((50, 100, 350, 350), fill=(100, 70, 40, 255))
    image.putpixel((49, 100), (100, 70, 40, 1))
    cleaned = clean_photoroom_cutout(image)
    assert ImageChops.difference(cleaned.convert("RGB"), image.convert("RGB")).getbbox() is None
    assert cleaned.getpixel((49, 100))[3] == 1
    assert cleaned.getpixel((0, 0))[3] == 0
    assert cleaned.getpixel((50, 100)) == image.getpixel((50, 100))
    _, placement = compose(Image.new("RGB", CANVAS), cleaned, "大")
    assert placement["resize_to"][0] == 972


def test_four_template_workers_prepare_foreground_once_and_retry_only_missing(studio_app, monkeypatch):
    import threading
    import app as app_module
    client = studio_app.test_client()
    upload(client, "templates", count=8)
    studio = studio_app.extensions["studio"]
    studio.run_job(upload(client, "products").json["job_id"])
    product = client.get("/api/state").json["products"][0]
    data = client.get(f"/api/products/{product['id']}/alignment").json
    job = client.post(f"/api/products/{product['id']}/confirm-placement", json={
        "revision": data["revision"], "template_id": data["template_id"], "alignment": data["alignment"]}).json["job_id"]
    original = studio.compose_template
    barrier = threading.Barrier(4, timeout=10)
    lock = threading.Lock()
    threads, calls = set(), []
    broken = studio.rows("SELECT id FROM templates")[0]["id"]
    def worker(product, template, layer, placement):
        with lock:
            threads.add(threading.get_ident()); calls.append(template["id"]); index = len(calls)
        if index <= 4:
            barrier.wait()
        if template["id"] == broken:
            raise ValueError("one broken template")
        return original(product, template, layer, placement)
    monkeypatch.setattr(studio, "compose_template", worker)
    spy = Mock(wraps=app_module.compose)
    monkeypatch.setattr(app_module, "compose", spy)
    studio.run_job(job)
    assert len(threads) == 4 and len(calls) == 8
    assert spy.call_count == 1
    assert studio.one("SELECT status FROM jobs WHERE id=?", (job,))["status"] == "error"
    rows = studio.rows("SELECT * FROM composites")
    assert len(rows) == 7
    assert len({json.loads(c["placement"])["product_layer"] for c in rows}) == 1
    row = rows[0]
    template = studio.one("SELECT file FROM templates WHERE id=?", (row["template_id"],))
    expected, _ = compose(read_image(studio.media / template["file"]), cutout(None), "中", data["alignment"])
    assert ImageChops.difference(expected.convert("RGB"), read_image(studio.media / row["file"]).convert("RGB")).getbbox() is None
    calls.clear()
    def retry_worker(product, template, layer, placement):
        calls.append(template["id"])
        return original(product, template, layer, placement)
    monkeypatch.setattr(studio, "compose_template", retry_worker)
    retry = client.post(f"/api/jobs/{job}/retry", json={}).json["job_id"]
    studio.run_job(retry)
    assert calls == [broken] and len(studio.rows("SELECT id FROM composites")) == 8
    before = {r["id"] for r in studio.rows("SELECT id FROM composites")}
    studio.compose_product(product["id"])
    assert {r["id"] for r in studio.rows("SELECT id FROM composites")} == before


def test_poof_wired_to_upload_and_recut_preserves_old_history(studio_app, monkeypatch):
    import httpx
    import app as app_module
    import poof_service
    client = studio_app.test_client()
    studio = studio_app.extensions["studio"]
    studio_app.config["REMOVER"] = None
    monkeypatch.setenv("POOF_API_KEY", "test-poof-key")
    requests = []
    response = io.BytesIO()
    cutout(None).save(response, "PNG")
    def handler(request):
        assert str(request.url) == poof_service.ENDPOINT
        requests.append(request)
        return httpx.Response(200, content=response.getvalue())
    real_client = httpx.Client
    monkeypatch.setattr(poof_service.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    studio.run_job(upload(client, "products").json["job_id"])
    state = client.get("/api/state").json
    assert state["removal_provider"] == "poof" and state["removal_ready"] is True
    product = state["products"][0]
    assert product["cutout_provider"] == "poof" and product["status"] == "ready"
    old_cutout = product["cutout"]
    studio.write("UPDATE products SET cutout_provider='photoroom' WHERE id=?", (product["id"],))
    job = client.post("/api/products/remove-background", json={"ids": [product["id"]]}).json["job_id"]
    studio.run_job(job)
    current = client.get("/api/state").json["products"][0]
    assert current["cutout_provider"] == "poof" and current["cutout"] != old_cutout
    assert current["placement_confirmed"] == 0 and (studio.media / old_cutout).exists()
    assert len(requests) == 2
    assert "test-poof-key" not in studio.safe_error(ValueError("test-poof-key"))


def test_photoroom_option_uses_saved_original_and_keeps_history(studio_app, monkeypatch):
    import app as app_module
    client, state = prepared(studio_app, templates=1)
    studio = studio_app.extensions["studio"]
    product = state["products"][0]
    original = (studio.media / product["original"]).read_bytes()
    old_cutout = product["cutout"]
    old_composite = state["composites"][0]
    studio_app.config["REMOVER"] = None
    monkeypatch.setattr(app_module, "load_photoroom_key", lambda root: "test-photo-key")
    def photo(image, root):
        saved = read_image(io.BytesIO(original))
        assert image.size == saved.size and image.tobytes() == saved.tobytes()
        return cutout(None)
    photo_spy = Mock(side_effect=photo)
    monkeypatch.setattr(app_module, "remove_with_photoroom", photo_spy)
    monkeypatch.setattr(app_module, "remove_background", lambda *args: pytest.fail("Explicit Photoroom must not call Poof"))
    response = client.post("/api/products/remove-background", json={"ids": [product["id"]], "provider": "photoroom"})
    assert response.status_code == 202
    settings = json.loads(studio.one("SELECT settings FROM jobs WHERE id=?", (response.json["job_id"],))["settings"])
    assert settings["providers"] == {product["id"]: "photoroom"}
    studio.run_job(response.json["job_id"])
    current = client.get("/api/state").json["products"][0]
    assert current["cutout_provider"] == "photoroom" and current["remove_provider"] == "photoroom"
    assert current["status"] == "ready" and not current["placement_confirmed"]
    assert current["cutout"] != old_cutout
    assert (studio.media / old_cutout).exists() and (studio.media / old_composite["file"]).exists()
    assert (studio.media / product["original"]).read_bytes() == original
    assert client.get(f"/api/download/original/{product['id']}").data == original
    assert photo_spy.call_count == 1


@pytest.mark.parametrize("retry_kind", ["product", "job"])
def test_photoroom_failure_retry_keeps_selected_provider(studio_app, monkeypatch, retry_kind):
    import app as app_module
    client, state = prepared(studio_app, templates=1)
    studio = studio_app.extensions["studio"]
    product = state["products"][0]
    studio_app.config["REMOVER"] = None
    monkeypatch.setattr(app_module, "load_photoroom_key", lambda root: "test-photo-key")
    photo = Mock(side_effect=[ValueError("temporary photo failure"), cutout(None)])
    monkeypatch.setattr(app_module, "remove_with_photoroom", photo)
    monkeypatch.setattr(app_module, "remove_background", lambda *args: pytest.fail("Retry must keep Photoroom"))
    job = client.post("/api/products/remove-background", json={"ids": [product["id"]], "provider": "photoroom"}).json["job_id"]
    studio.run_job(job)
    assert studio.one("SELECT status FROM products WHERE id=?", (product["id"],))["status"] == "error"
    assert (studio.media / product["cutout"]).exists()
    if retry_kind == "product":
        retry = client.post("/api/products/retry", json={"ids": [product["id"]]})
    else:
        # A historical job carries its own choice even if the product has since
        # been set to use a different provider.
        studio.write("UPDATE products SET remove_provider='poof' WHERE id=?", (product["id"],))
        retry = client.post(f"/api/jobs/{job}/retry", json={})
    assert retry.status_code in (200, 202)
    studio.run_job(retry.json["job_id"])
    current = client.get("/api/state").json["products"][0]
    assert current["status"] == "ready" and current["cutout_provider"] == "photoroom"
    assert photo.call_count == 2


def test_recut_rejects_invalid_provider_or_missing_key_before_changes(studio_app, monkeypatch):
    import app as app_module
    client, state = prepared(studio_app, templates=1)
    studio = studio_app.extensions["studio"]
    product = state["products"][0]
    studio_app.config["REMOVER"] = None
    monkeypatch.setattr(app_module, "load_photoroom_key", lambda root: "")
    before = studio.rows("SELECT * FROM products")
    jobs = studio.rows("SELECT id FROM jobs")
    for provider in ("photoroom", "unknown", ["photoroom"]):
        response = client.post("/api/products/remove-background", json={"ids": [product["id"]], "provider": provider})
        assert response.status_code == 400
    assert studio.rows("SELECT * FROM products") == before
    assert studio.rows("SELECT id FROM jobs") == jobs
