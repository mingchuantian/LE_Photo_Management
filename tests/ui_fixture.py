"""Disposable UI demo on port 5001. Never calls OpenAI; not production data."""
import io
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from app import create_app
from waitress import serve

directory = Path(__file__).resolve().parents[1] / ".test-artifacts" / "ui-grouped"


def bag(color):
    image = Image.new("RGBA", (550, 650))
    d = ImageDraw.Draw(image)
    d.arc((190, 65, 360, 305), 180, 360, fill=color, width=20)
    d.rounded_rectangle((90, 215, 460, 520), radius=26, fill=color, outline="#573b2c", width=3)
    d.line((115, 242, 435, 242), fill="#ca9470", width=2)
    d.line((130, 465, 420, 465), fill="#ca9470", width=2)
    d.rounded_rectangle((255, 245, 295, 283), radius=5, fill="#e9c284")
    return image


def encode(image):
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


app = create_app({"DATA_DIR": directory, "DISABLE_WORKERS": True,
                  "REMOVER": lambda image: bag("#936140"),
                  "EDITOR": lambda path, n, root: [Path(path).read_bytes() for _ in range(n)]})
studio = app.extensions["studio"]
client = app.test_client()
if not studio.rows("SELECT id FROM products"):
    backgrounds = []
    for i, color in enumerate(["#e9e4d7", "#dce4d5", "#e6d9ce", "#e1e3dd", "#dbdfcb", "#e5dcd3"] * 6):
        image = Image.new("RGB", (1080, 1350), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 945, 1080, 1350), fill=["#d9c8ae", "#c7cbbb", "#d5bdae"][i % 3])
        draw.line((0, 945, 1080, 945), fill="#c2b8a7", width=2)
        draw.rounded_rectangle((858, 815, 997, 1010), radius=12, fill="#cac2ab")
        draw.line((920, 900, 930, 590), fill="#677657", width=7)
        for x,y in [(900,700),(935,640),(916,790),(950,750)]:
            draw.ellipse((x-45,y-55,x+27,y+25), fill="#839373")
        backgrounds.append((io.BytesIO(encode(image)), f"示例背景 {i+1:02d}.png"))
    client.post("/api/templates", data={"files": backgrounds})
    for name,color in [("示例 · 焦糖手提包.png", "#936140"), ("示例 · 巧克力包.png", "#594332")]:
        original = Image.new("RGBA", (550,650), "#eae8df")
        original.alpha_composite(bag(color))
        response = client.post("/api/products", data={"files": (io.BytesIO(encode(original)),name)})
        studio.run_job(response.json["job_id"])
    for product in client.get("/api/state").json["products"]:
        draft = client.get(f"/api/products/{product['id']}/alignment").json
        response = client.post(f"/api/products/{product['id']}/confirm-placement", json={
            "revision": draft["revision"], "template_id": draft["template_id"], "alignment": draft["alignment"]})
        studio.run_job(response.json["job_id"])
    composite_ids = [c["id"] for c in studio.rows("SELECT id FROM composites LIMIT 2")]
    response = client.post("/api/generate", json={"ids":composite_ids, "n":3})
    studio.run_job(response.json["job_id"])
app.config["DISABLE_WORKERS"] = False
print("Disposable UI fixture → http://127.0.0.1:5001 (mock AI)", flush=True)
serve(app,host="127.0.0.1",port=5001,threads=4)
