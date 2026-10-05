from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException

from ai_service import PROMPT, SETTINGS, edit_images, load_key
from photoroom_service import load_photoroom_key, remove_background
from imaging import (BASELINE, CANVAS, LAYOUT_SIGNATURE, SIZE_WIDTHS, compose,
                     normalize_template, product_layer, read_image, restore_product, save_image, validate_alignment)

ROOT = Path(__file__).resolve().parent


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, original TEXT NOT NULL, cutout TEXT,
 size TEXT NOT NULL DEFAULT '中', revision INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'queued', error TEXT, created TEXT NOT NULL, deleted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS templates (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, file TEXT NOT NULL,
 source_width INTEGER, source_height INTEGER, created TEXT NOT NULL, deleted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS composites (
 id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id),
 template_id TEXT NOT NULL REFERENCES templates(id), revision INTEGER NOT NULL,
 size TEXT NOT NULL, file TEXT NOT NULL, placement TEXT NOT NULL, created TEXT NOT NULL,
 UNIQUE(product_id, template_id, revision)
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 n INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, finished TEXT, settings TEXT
);
CREATE TABLE IF NOT EXISTS job_items (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ref_id TEXT NOT NULL,
 status TEXT NOT NULL, error TEXT, UNIQUE(job_id, ref_id)
);
CREATE TABLE IF NOT EXISTS results (
 id TEXT PRIMARY KEY, composite_id TEXT NOT NULL REFERENCES composites(id),
 job_id TEXT NOT NULL REFERENCES jobs(id), file TEXT NOT NULL,
 variant INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, created TEXT NOT NULL,
 raw_file TEXT, product_restored INTEGER NOT NULL DEFAULT 0, raw_width INTEGER, raw_height INTEGER
);
CREATE TABLE IF NOT EXISTS studio_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS composite_product ON composites(product_id);
CREATE INDEX IF NOT EXISTS result_composite ON results(composite_id);
CREATE INDEX IF NOT EXISTS item_status ON job_items(status, ref_id);
"""


class Studio:
    def __init__(self, app):
        self.app = app
        self.directory = Path(app.config["DATA_DIR"]).resolve()
        self.media = self.directory / "media"
        self.media.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "studio.sqlite3"
        self.local = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-photo")
        self.ai = ThreadPoolExecutor(max_workers=2, thread_name_prefix="openai-photo")
        self.schedule_lock = threading.Lock()
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
            for column, declaration in {"alignment": "TEXT", "placement_confirmed": "INTEGER NOT NULL DEFAULT 0",
                                        "preview_template_id": "TEXT", "cutout_provider": "TEXT"}.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE products ADD COLUMN {column} {declaration}")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(results)")}
            for column, declaration in {"raw_file": "TEXT", "product_restored": "INTEGER NOT NULL DEFAULT 0",
                                        "raw_width": "INTEGER", "raw_height": "INTEGER"}.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE results ADD COLUMN {column} {declaration}")
            # A restarted server must never silently re-submit billable jobs.
            db.execute("UPDATE job_items SET status='interrupted', error=? WHERE status IN ('queued','running')",
                       ("上次服务已停止，请手动重试。AI 请求可能已经计费，请先检查结果。",))
            db.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status IN ('queued','running')", (now(),))
            db.execute("UPDATE products SET status='error', error='上次 Photoroom 请求被中断，请先检查后手动重试，可能已计费。' WHERE status IN ('queued','running')")
            previous_layout = db.execute("SELECT value FROM studio_meta WHERE key='layout'").fetchone()
            if previous_layout is None or previous_layout["value"] != LAYOUT_SIGNATURE:
                db.execute("UPDATE products SET revision=revision+1 WHERE deleted=0")
                db.execute("INSERT OR REPLACE INTO studio_meta VALUES ('layout',?)", (LAYOUT_SIGNATURE,))
        # These are local-only repairs. Never re-submit historical AI requests.
        self.queue("restore", [r["id"] for r in self.rows("SELECT id FROM results WHERE product_restored=0")])
        missing = self.rows("""SELECT p.id FROM products p WHERE p.deleted=0 AND p.status='ready' AND p.placement_confirmed=1
            AND EXISTS (SELECT 1 FROM templates t WHERE t.deleted=0 AND NOT EXISTS
              (SELECT 1 FROM composites c WHERE c.product_id=p.id AND c.template_id=t.id AND c.revision=p.revision))""")
        self.queue("compose", [p["id"] for p in missing])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, query, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, args).fetchall()]

    def one(self, query, args=()):
        rows = self.rows(query, args)
        return rows[0] if rows else None

    def write(self, query, args=()):
        with self.connect() as db:
            db.execute(query, args)

    def safe_error(self, error):
        # API error text can contain request information; scrub credentials.
        message = str(error)
        for key in (load_key(ROOT), load_photoroom_key(ROOT)):
            if key:
                message = message.replace(key, "[key hidden]")
        message = re.sub(r"sk-[A-Za-z0-9_\-]+", "[key hidden]", message)
        return message[:1200] or "处理失败，请重试。"

    def queue(self, kind, references, n=0):
        references = list(dict.fromkeys(references))
        if not references:
            return None
        with self.schedule_lock:
            with self.connect() as db:
                active = {row[0] for row in db.execute(
                    "SELECT i.ref_id FROM job_items i JOIN jobs j ON i.job_id=j.id "
                    "WHERE j.kind=? AND i.status IN ('queued','running')", (kind,))}
                # Local composition requests are cheap and serialized. Keeping a
                # follow-up guarantees size/template changes during a job appear.
                if kind != "compose":
                    references = [r for r in references if r not in active]
                if not references:
                    return None
                identifier = uid()
                settings = json.dumps({**SETTINGS, "prompt": PROMPT, "n": n}, ensure_ascii=False) if kind == "generate" else None
                db.execute("INSERT INTO jobs (id,kind,status,n,created,settings) VALUES (?,?,?,?,?,?)",
                           (identifier, kind, "queued", n, now(), settings))
                for ref in references:
                    db.execute("INSERT INTO job_items (id,job_id,ref_id,status) VALUES (?,?,?,?)",
                               (uid(), identifier, ref, "queued"))
                    if kind == "remove":
                        db.execute("UPDATE products SET status='queued',error=NULL,placement_confirmed=0 WHERE id=?", (ref,))
            if not self.app.config["DISABLE_WORKERS"]:
                (self.ai if kind == "generate" else self.local).submit(self.run_job, identifier)
        return identifier

    def remove(self, product_id):
        product = self.one("SELECT * FROM products WHERE id=? AND deleted=0", (product_id,))
        if not product:
            raise ValueError("商品已删除。")
        self.write("UPDATE products SET status='running',error=NULL WHERE id=?", (product_id,))
        image = read_image(self.media / product["original"])
        remover = self.app.config.get("REMOVER")
        if remover:
            output = remover(image)
        else:
            output = remove_background(image, ROOT)
        output = output.convert("RGBA")
        if not output.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox():
            raise ValueError("去背景后没有可见商品，请更换原图。")
        relative = f"cutouts/{product_id}_{uid()}.png"
        save_image(output, self.media / relative)
        self.write("UPDATE products SET cutout=?,status='ready',error=NULL,cutout_provider='photoroom',"
                   "placement_confirmed=0,alignment=NULL,revision=revision+1 WHERE id=?", (relative, product_id))

    def compose_product(self, product_id):
        product = self.one("SELECT * FROM products WHERE id=? AND status='ready' AND deleted=0 AND placement_confirmed=1", (product_id,))
        if not product:
            return
        cutout = read_image(self.media / product["cutout"])
        for template in self.rows("SELECT * FROM templates WHERE deleted=0"):
            if self.one("SELECT id FROM composites WHERE product_id=? AND template_id=? AND revision=?",
                        (product_id, template["id"], product["revision"])):
                continue
            image, placement = compose(read_image(self.media / template["file"]), cutout, product["size"],
                                       json.loads(product["alignment"]) if product["alignment"] else None)
            identifier = uid()
            relative = f"composites/{identifier}.png"
            save_image(image, self.media / relative)
            layer_file = f"composites/{identifier}_product.png"
            save_image(product_layer(cutout, placement), self.media / layer_file)
            placement["product_layer"] = layer_file
            self.write("INSERT OR IGNORE INTO composites VALUES (?,?,?,?,?,?,?,?)",
                       (identifier, product_id, template["id"], product["revision"], product["size"],
                        relative, json.dumps(placement), now()))

    def foreground(self, composite):
        placement = json.loads(composite["placement"])
        relative = placement.get("product_layer")
        if relative and (self.media / relative).exists():
            return read_image(self.media / relative)
        product = self.one("SELECT cutout FROM products WHERE id=?", (composite["product_id"],))
        if not product or not product["cutout"]:
            raise ValueError("商品透明图缺失，无法还原商品细节。")
        layer = product_layer(read_image(self.media / product["cutout"]), placement)
        relative = f"composites/{composite['id']}_product.png"
        save_image(layer, self.media / relative)
        placement["product_layer"] = relative
        self.write("UPDATE composites SET placement=? WHERE id=?", (json.dumps(placement), composite["id"]))
        return layer

    def restore_result(self, result_id):
        result = self.one("SELECT * FROM results WHERE id=?", (result_id,))
        if not result or result["product_restored"]:
            return
        composite = self.one("SELECT * FROM composites WHERE id=?", (result["composite_id"],))
        raw_file = result["raw_file"] or result["file"]
        raw = read_image(self.media / raw_file)
        restored = restore_product(raw, self.foreground(composite))
        relative = f"results/{result_id}_restored.png"
        save_image(restored, self.media / relative)
        self.write("UPDATE results SET file=?,width=?,height=?,raw_file=?,product_restored=1,raw_width=?,raw_height=? WHERE id=?",
                   (relative, *restored.size, raw_file, *raw.size, result_id))

    def generate(self, composite_id, job):
        composite = self.one("SELECT * FROM composites WHERE id=?", (composite_id,))
        if not composite:
            raise ValueError("找不到合成图。")
        # Freeze the foreground BEFORE the API request; later size/cutout changes
        # cannot change the product restored into this particular generation.
        layer = self.foreground(composite)
        editor = self.app.config.get("EDITOR", edit_images)
        images = editor(self.media / composite["file"], job["n"], ROOT)
        for index, content in enumerate(images, 1):
            raw = read_image(io.BytesIO(content))
            image = restore_product(raw, layer)
            identifier = uid()
            relative = f"results/{identifier}.png"
            raw_file = f"ai_raw/{identifier}.png"
            save_image(raw, self.media / raw_file)
            save_image(image, self.media / relative)
            self.write("INSERT INTO results (id,composite_id,job_id,file,variant,width,height,created,raw_file,product_restored,raw_width,raw_height) "
                       "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                       (identifier, composite_id, job["id"], relative, index, *image.size, now(), raw_file, *raw.size))
        if len(images) != job["n"]:
            raise ValueError(f"请求 {job['n']} 张，实际收到 {len(images)} 张，已保存收到的结果。")

    def run_job(self, job_id):
        job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.write("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        for item in self.rows("SELECT * FROM job_items WHERE job_id=? AND status='queued'", (job_id,)):
            self.write("UPDATE job_items SET status='running' WHERE id=?", (item["id"],))
            try:
                if job["kind"] == "remove":
                    self.remove(item["ref_id"])
                elif job["kind"] == "compose":
                    self.compose_product(item["ref_id"])
                elif job["kind"] == "restore":
                    self.restore_result(item["ref_id"])
                else:
                    self.generate(item["ref_id"], job)
                self.write("UPDATE job_items SET status='done',error=NULL WHERE id=?", (item["id"],))
            except Exception as error:
                message = self.safe_error(error)
                self.write("UPDATE job_items SET status='error',error=? WHERE id=?", (message, item["id"]))
                if job["kind"] == "remove":
                    self.write("UPDATE products SET status='error',error=? WHERE id=?", (message, item["ref_id"]))
        items = self.rows("SELECT status FROM job_items WHERE job_id=?", (job_id,))
        failures = sum(item["status"] != "done" for item in items)
        status = "done" if not failures else ("error" if failures == len(items) else "partial")
        self.write("UPDATE jobs SET status=?,finished=? WHERE id=?", (status, now(), job_id))


def create_app(config=None):
    app = Flask(__name__)
    app.json.sort_keys = False  # Preserve the five sizes in small → large order.
    app.config.update(DATA_DIR=os.getenv("PHOTO_DATA_DIR", str(ROOT / "data")),
                      MAX_CONTENT_LENGTH=256 * 1024 * 1024, DISABLE_WORKERS=False)
    if config:
        app.config.update(config)
    studio = Studio(app)
    app.extensions["studio"] = studio

    @app.before_request
    def local_write_guard():
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            # Reject cross-origin requests from unrelated websites into localhost.
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                abort(403, description="不接受跨站写入。")

    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error, HTTPException):
            if error.code == 413:
                return jsonify(error="本次上传超过 256 MB，请分批上传。"), 413
            return jsonify(error=error.description), error.code
        if isinstance(error, (ValueError, UnidentifiedImageError, Image.DecompressionBombError)):
            return jsonify(error=studio.safe_error(error)), 400
        app.logger.error("Request failed: %s", studio.safe_error(error))
        return jsonify(error="处理失败，请检查本地服务或重试。"), 500

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        products = studio.rows("SELECT * FROM products WHERE deleted=0 ORDER BY created DESC")
        templates = studio.rows("SELECT * FROM templates WHERE deleted=0 ORDER BY created DESC")
        composites = studio.rows("""SELECT c.*,p.name product_name,t.name template_name,
            CASE WHEN p.revision=c.revision AND t.deleted=0 AND p.placement_confirmed=1 AND p.status='ready' THEN 1 ELSE 0 END active
            FROM composites c JOIN products p ON p.id=c.product_id JOIN templates t ON t.id=c.template_id
            WHERE p.deleted=0 ORDER BY c.created DESC""")
        results = studio.rows("""SELECT r.*,c.product_id,c.template_id,c.size,p.name product_name,t.name template_name
            FROM results r JOIN composites c ON r.composite_id=c.id JOIN products p ON p.id=c.product_id
            JOIN templates t ON t.id=c.template_id WHERE p.deleted=0 ORDER BY r.created DESC""")
        jobs = studio.rows("SELECT * FROM jobs WHERE status IN ('queued','running') OR id IN "
                           "(SELECT id FROM jobs ORDER BY created DESC LIMIT 40) ORDER BY created DESC")
        if jobs:
            placeholders = ",".join("?" for _ in jobs)
            items = studio.rows(f"SELECT * FROM job_items WHERE job_id IN ({placeholders})", [j["id"] for j in jobs])
            for job in jobs:
                job["items"] = [item for item in items if item["job_id"] == job["id"]]
                job.pop("settings", None)
        return jsonify(products=products, templates=templates, composites=composites,
                       results=results, jobs=jobs, key_ready=bool(load_key(ROOT)),
                       photoroom_ready=bool(load_photoroom_key(ROOT)), removal_provider="photoroom",
                       sizes=SIZE_WIDTHS, baseline=BASELINE, canvas=CANVAS, settings=SETTINGS, prompt=PROMPT)

    def upload(kind):
        if kind == "products" and not load_photoroom_key(ROOT) and not app.config.get("REMOVER"):
            raise ValueError("找不到 Photoroom key，请放入根目录 photoroom_key 后上传。")
        files = request.files.getlist("files")
        if not files:
            raise ValueError("请至少选择一张图片。")
        if len(files) > 100:
            raise ValueError("每次最多上传 100 张图片。")
        accepted, rejected = [], []
        for file in files:
            name = Path(file.filename or "未命名图片").name[:160]
            try:
                file.stream.seek(0, 2)
                if file.stream.tell() > 30 * 1024 * 1024:
                    raise ValueError("单张图片不能超过 30 MB。")
                file.stream.seek(0)
                image = read_image(file.stream)
                identifier = uid()
                if kind == "products":
                    relative = f"originals/{identifier}.png"
                    save_image(image, studio.media / relative)
                    studio.write("INSERT INTO products (id,name,original,created) VALUES (?,?,?,?)",
                                 (identifier, name, relative, now()))
                else:
                    relative = f"templates/{identifier}.png"
                    save_image(normalize_template(image), studio.media / relative)
                    studio.write("INSERT INTO templates (id,name,file,source_width,source_height,created) VALUES (?,?,?,?,?,?)",
                                 (identifier, name, relative, *image.size, now()))
                accepted.append(identifier)
            except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
                rejected.append({"name": name, "error": studio.safe_error(error)})
        job_id = None
        if accepted:
            if kind == "products":
                job_id = studio.queue("remove", accepted)
            else:
                job_id = studio.queue("compose", [p["id"] for p in studio.rows("SELECT id FROM products WHERE status='ready' AND deleted=0 AND placement_confirmed=1")])
        return jsonify(accepted=accepted, rejected=rejected, job_id=job_id), 201 if accepted else 400

    @app.post("/api/products")
    def upload_products():
        return upload("products")

    @app.post("/api/templates")
    def upload_templates():
        return upload("templates")

    def payload():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求需要 JSON 对象。")
        return body

    def ids(body, field="ids"):
        values = body.get(field)
        if not isinstance(values, list) or not values or len(values) > 1000 or any(not isinstance(i, str) for i in values):
            raise ValueError("请选择 1–1000 个项目。")
        return list(dict.fromkeys(values))

    def require_rows(table, identifiers, active=True):
        clause = " AND deleted=0" if active and table in {"products", "templates"} else ""
        rows = studio.rows(f"SELECT * FROM {table} WHERE id IN ({','.join('?' for _ in identifiers)}){clause}", identifiers)
        if len(rows) != len(identifiers):
            raise ValueError("部分项目已不存在，请刷新页面。")
        return rows

    @app.patch("/api/products/size")
    def change_size():
        body = payload()
        identifiers = ids(body)
        size = body.get("size")
        if size not in SIZE_WIDTHS:
            raise ValueError("请选择有效的包包尺寸。")
        require_rows("products", identifiers)
        with studio.connect() as db:
            for identifier in identifiers:
                db.execute("UPDATE products SET size=?,revision=revision+1,alignment=NULL,placement_confirmed=0 WHERE id=? AND size<>?", (size, identifier, size))
        return jsonify(job_id=None)

    def ready_product(identifier):
        product = require_rows("products", [identifier])[0]
        if product["status"] != "ready" or not product["cutout"]:
            raise ValueError("请等待商品抠图完成，再调整摆放。")
        return product

    def checked_placement(identifier, body):
        product = ready_product(identifier)
        if type(body.get("revision")) is not int or body["revision"] != product["revision"]:
            raise ValueError("商品已更新，请重新打开摆放预览。")
        if not isinstance(body.get("template_id"), str):
            raise ValueError("请选择一个背景模板。")
        template = require_rows("templates", [body["template_id"]])[0]
        alignment = validate_alignment(body.get("alignment"))
        image, placement = compose(read_image(studio.media / template["file"]),
                                   read_image(studio.media / product["cutout"]), product["size"], alignment)
        return product, template, alignment, image, placement

    @app.get("/api/products/<identifier>/alignment")
    def alignment_data(identifier):
        product = ready_product(identifier)
        templates = studio.rows("SELECT id FROM templates WHERE deleted=0 ORDER BY created DESC")
        if not templates:
            raise ValueError("请先上传一个背景模板。")
        cutout = read_image(studio.media / product["cutout"])
        box = cutout.getchannel("A").getbbox()
        sprite_file = str(Path(product["cutout"]).with_name(Path(product["cutout"]).stem + "_sprite.png")).replace("\\", "/")
        if not (studio.media / sprite_file).exists():
            save_image(cutout.crop(box), studio.media / sprite_file)
        _, default = compose(Image.new("RGB", CANVAS), cutout, product["size"])
        initial = {"cx": (default["x"] + default["width"] / 2) / CANVAS[0],
                   "cy": (default["y"] + default["height"] / 2) / CANVAS[1],
                   "width": default["resize_to"][0] / CANVAS[0], "angle": 0}
        template_ids = {t["id"] for t in templates}
        return jsonify(product_id=identifier, revision=product["revision"], sprite_file=sprite_file,
                       source_width=box[2] - box[0], source_height=box[3] - box[1],
                       alignment=json.loads(product["alignment"]) if product["alignment"] else initial,
                       defaults=initial, template_id=product["preview_template_id"] if product["preview_template_id"] in template_ids else templates[0]["id"])

    @app.post("/api/products/<identifier>/preview")
    def placement_preview(identifier):
        _, _, _, image, _ = checked_placement(identifier, payload())
        stream = io.BytesIO()
        image.save(stream, "PNG")
        stream.seek(0)
        response = send_file(stream, mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/products/<identifier>/confirm-placement")
    def confirm_placement(identifier):
        product, template, alignment, _, _ = checked_placement(identifier, payload())
        with studio.connect() as db:
            changed = db.execute("UPDATE products SET alignment=?,preview_template_id=?,placement_confirmed=1,"
                                 "revision=revision+1 WHERE id=? AND revision=? AND status='ready' AND deleted=0",
                                 (json.dumps(alignment), template["id"], identifier, product["revision"]))
            if changed.rowcount != 1:
                raise ValueError("商品已更新，请重新打开摆放预览。")
        return jsonify(job_id=studio.queue("compose", [identifier]))

    @app.post("/api/products/remove-background")
    def reprocess_products():
        identifiers = ids(payload())
        require_rows("products", identifiers)
        if not load_photoroom_key(ROOT) and not app.config.get("REMOVER"):
            raise ValueError("找不到 Photoroom key，请放入根目录 photoroom_key。")
        return jsonify(job_id=studio.queue("remove", identifiers)), 202

    @app.post("/api/products/retry")
    def retry_remove():
        identifiers = ids(payload())
        rows = require_rows("products", identifiers)
        if any(row["status"] != "error" for row in rows):
            raise ValueError("只有失败的商品需要重新去背景。")
        if not load_photoroom_key(ROOT) and not app.config.get("REMOVER"):
            raise ValueError("找不到 Photoroom key，请放入根目录 photoroom_key。")
        return jsonify(job_id=studio.queue("remove", identifiers))

    @app.delete("/api/templates/<identifier>")
    def delete_template(identifier):
        require_rows("templates", [identifier])
        studio.write("UPDATE templates SET deleted=1 WHERE id=?", (identifier,))
        return jsonify(ok=True)

    @app.delete("/api/products/<identifier>")
    def delete_product(identifier):
        require_rows("products", [identifier])
        studio.write("UPDATE products SET deleted=1 WHERE id=?", (identifier,))
        return jsonify(ok=True)

    @app.post("/api/generate")
    def generate():
        body = payload()
        identifiers = ids(body)
        n = body.get("n", 3)
        if type(n) is not int or not 1 <= n <= 10:
            raise ValueError("生成数量必须为 1–10 的整数。")
        composites = require_rows("composites", identifiers)
        for composite in composites:
            product = studio.one("SELECT * FROM products WHERE id=? AND deleted=0", (composite["product_id"],))
            template = studio.one("SELECT * FROM templates WHERE id=? AND deleted=0", (composite["template_id"],))
            if not product or not template or product["revision"] != composite["revision"] or not product["placement_confirmed"] or product["status"] != "ready":
                raise ValueError("尺寸或模板已变更，请使用当前合成预览。")
        if not load_key(ROOT) and not app.config.get("EDITOR"):
            raise ValueError("找不到 OpenAI key，请放入根目录 openai_key。")
        return jsonify(job_id=studio.queue("generate", identifiers, n)), 202

    @app.post("/api/jobs/<identifier>/retry")
    def retry_job(identifier):
        job = studio.one("SELECT * FROM jobs WHERE id=?", (identifier,))
        if not job:
            abort(404)
        failed = studio.rows("SELECT ref_id FROM job_items WHERE job_id=? AND status IN ('error','interrupted')", (identifier,))
        if not failed:
            raise ValueError("没有失败或中断的项目。")
        references = [item["ref_id"] for item in failed]
        if job["kind"] in {"remove", "compose"}:
            references = [r for r in references if studio.one("SELECT id FROM products WHERE id=? AND deleted=0", (r,))]
        return jsonify(job_id=studio.queue(job["kind"], references, job["n"])), 202

    @app.get("/media/<path:filename>")
    def media(filename):
        # Data DB, API key and other application files are outside this directory.
        return send_from_directory(studio.media, filename, conditional=True, max_age=86400)

    @app.get("/api/download/<kind>/<identifier>")
    def download_one(kind, identifier):
        mapping = {"result": ("results", "file"), "composite": ("composites", "file"),
                   "raw-result": ("results", "raw_file"),
                   "cutout": ("products", "cutout"), "original": ("products", "original")}
        if kind not in mapping:
            abort(404)
        table, column = mapping[kind]
        row = studio.one(f"SELECT * FROM {table} WHERE id=?", (identifier,))
        if not row or not row[column]:
            abort(404)
        return send_file(studio.media / row[column], as_attachment=True, download_name=f"{kind}_{identifier[:8]}.png")

    @app.post("/api/download")
    def download_zip():
        body = payload()
        identifiers = ids(body)
        kind = body.get("kind", "results")
        mapping = {"results": ("results", "file"), "composites": ("composites", "file"), "cutouts": ("products", "cutout")}
        if kind not in mapping:
            raise ValueError("下载类型无效。")
        table, column = mapping[kind]
        rows = require_rows(table, identifiers)
        # Use a disk-backed temporary stream for large batches, closed on response.
        import tempfile
        stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, row in enumerate(rows, 1):
                if not row[column]:
                    stream.close()
                    raise ValueError("部分商品尚未完成去背景。")
                archive.write(studio.media / row[column], f"{index:03d}_{kind}_{row['id'][:8]}.png")
        stream.seek(0)
        response = send_file(stream, mimetype="application/zip", as_attachment=True,
                             download_name=f"LE_{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
        response.call_on_close(stream.close)
        return response

    return app


if __name__ == "__main__":
    import sys
    import time
    import urllib.request
    import webbrowser
    from waitress import serve
    port = int(os.getenv("PORT", "5000"))
    address = f"http://127.0.0.1:{port}"
    if "--open" in sys.argv:
        try:
            with urllib.request.urlopen(address, timeout=2) as response:
                existing = "LE · 商品图工作室" in response.read().decode("utf-8")
            if existing:
                webbrowser.open(address)
                print("Studio is already running.", flush=True)
                sys.exit(0)
        except OSError:
            pass

        def open_when_ready():
            for _ in range(80):
                try:
                    with urllib.request.urlopen(address, timeout=1):
                        webbrowser.open(address)
                        return
                except OSError:
                    time.sleep(0.25)

        threading.Thread(target=open_when_ready, daemon=True).start()
    application = create_app()
    print(f"LE Photo Studio -> {address}", flush=True)
    serve(application, host="127.0.0.1", port=port, threads=8)
