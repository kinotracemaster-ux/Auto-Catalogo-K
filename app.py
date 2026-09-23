"""Generador de catalogo en PDF.

Pegas una lista de codigos -> la app busca en la carpeta de Drive la foto cuyo nombre es
ese codigo -> arma un PDF con las fotos y avisa cuales codigos no tienen foto.

Arranque local:   uvicorn app:app --reload
Variables de entorno: ver .env.example
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from pydantic import BaseModel, Field

from pdf_builder import LAYOUTS, Item, build_pdf, prepare_image
from sources import PhotoIndex, SourceError, make_source_from_env, norm_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("catalogo")

BASE = Path(__file__).parent
STATIC = BASE / "static"
LOGO = STATIC / "logo.png"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


APP_NAME = os.getenv("APP_NAME", "Generador de catálogo")
TITLE = os.getenv("CATALOG_TITLE", "Catálogo")
BRAND_NAME = os.getenv("BRAND_NAME", "")
BRAND_COLOR = os.getenv("BRAND_COLOR", "#E41F29")
if not re.fullmatch(r"#[0-9A-Fa-f]{6}", BRAND_COLOR):
    BRAND_COLOR = "#E41F29"
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "")
ACCESS_CODE = os.getenv("ACCESS_CODE", "")
MAX_CODES = _int("MAX_CODES", 150)
IMG_MAX_PX = _int("IMG_MAX_PX", 900)
JPEG_QUALITY = _int("JPEG_QUALITY", 80)
INDEX_TTL = _int("INDEX_TTL_SECONDS", 1800)
PDF_TTL_HOURS = _int("PDF_TTL_HOURS", 24)

CO_TZ = dt.timezone(dt.timedelta(hours=-5))  # Colombia no tiene horario de verano
TMP = Path(tempfile.gettempdir())
CACHE_DIR = TMP / "catalogo_cache"
PDF_DIR = TMP / "catalogo_pdfs"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)



def warm_index() -> None:
    """Lista el Drive apenas arranca la app, asi la primera busqueda no espera."""
    try:
        get_index().get()
    except Exception:  # noqa: BLE001
        log.warning("No pude listar el Drive al arrancar", exc_info=True)


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=warm_index, name="cargar-indice", daemon=True).start()
    yield


app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# --------------------------------------------------------------------------- helpers
_index: PhotoIndex | None = None
_index_lock = threading.Lock()


def get_index() -> PhotoIndex:
    """Se crea la primera vez que se usa, asi la app arranca aunque falte configurar el Drive."""
    global _index
    with _index_lock:
        if _index is None:
            _index = PhotoIndex(make_source_from_env(), ttl=INDEX_TTL)
        return _index


def authorized(code: str) -> bool:
    return not ACCESS_CODE or hmac.compare_digest((code or "").encode(), ACCESS_CODE.encode())


def parse_codes(text: str) -> list[dict]:
    """Un codigo por linea (o separados por coma/espacio). Opcional: 'CODIGO | texto' para ponerle un texto debajo."""
    items, seen = [], set()
    for line in (text or "").replace("\r", "\n").split("\n"):
        line = line.strip().lstrip("-•*·\"' ").strip()
        if not line:
            continue
        if "|" in line:
            code, note = line.split("|", 1)
            pairs = [(code, note.strip())]
        else:
            pairs = [(t, "") for t in re.split(r"[,;\t\s]+", line) if t]
        for code, note in pairs:
            code = code.strip().strip("\"'").rstrip(".,;:")
            key = norm_key(code)
            if key and key not in seen:
                seen.add(key)
                items.append({"code": code, "key": key, "note": note[:140]})
    return items


def load_prepared(source, photo) -> tuple[bytes, int, int]:
    """Foto lista para el PDF, con cache en disco (Drive es lento: la segunda vez es instantaneo)."""
    path = CACHE_DIR / f"{photo.cache_id}-{IMG_MAX_PX}-{JPEG_QUALITY}.jpg"
    if path.is_file():
        data = path.read_bytes()
        with Image.open(BytesIO(data)) as im:
            return data, im.width, im.height
    data, w, h = prepare_image(source.fetch(photo), IMG_MAX_PX, JPEG_QUALITY)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data, w, h


_last_cleanup = 0.0


def cleanup() -> None:
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 600:
        return
    _last_cleanup = now
    try:
        for f in PDF_DIR.glob("*.pdf"):
            if now - f.stat().st_mtime > PDF_TTL_HOURS * 3600:
                f.unlink(missing_ok=True)
        for f in CACHE_DIR.glob("*"):
            if now - f.stat().st_mtime > 7 * 24 * 3600:
                f.unlink(missing_ok=True)
    except OSError:
        log.exception("Limpieza de archivos temporales")


def slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "catalogo"


# --------------------------------------------------------------------------- API
class BuildRequest(BaseModel):
    codes: str = Field("", max_length=20000)
    title: str = Field("", max_length=80)
    per_page: int = 6
    show_code: bool = True


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    return {
        "app_name": APP_NAME,
        "title": TITLE,
        "brand_color": BRAND_COLOR,
        "requires_code": bool(ACCESS_CODE),
        "max_codes": MAX_CODES,
        "layouts": list(LAYOUTS),
        "has_logo": LOGO.is_file(),
    }


@app.get("/api/status")
def status(x_access_code: str = Header(default="")):
    """Sirve para comprobar la conexion con el Drive la primera vez."""
    out: dict = {"ok": False}
    try:
        idx = get_index()
        idx.get()
        out.update(ok=True, photos=idx.photo_count)
        if authorized(x_access_code):
            out["source"] = idx.source.describe()
            out["sample"] = idx.sample()
    except SourceError as exc:
        out["error"] = str(exc)
    except Exception:  # noqa: BLE001
        log.exception("status")
        out["error"] = "No pude conectar con el Drive."
    return out


_slots = threading.BoundedSemaphore(2)  # como mucho 2 PDF a la vez, para no ahogar el servidor


@app.post("/api/build")
def build(req: BuildRequest, x_access_code: str = Header(default="")):
    if not authorized(x_access_code):
        raise HTTPException(401, "Clave de acceso incorrecta.")
    items = parse_codes(req.codes)
    if not items:
        return {"ok": False, "message": "Pega al menos un código."}
    if len(items) > MAX_CODES:
        return {"ok": False, "message": f"Máximo {MAX_CODES} códigos por PDF (pegaste {len(items)}). Divide la lista en dos."}
    if not _slots.acquire(timeout=90):
        raise HTTPException(503, "Hay muchos pedidos a la vez. Intenta de nuevo en un minuto.")
    try:
        cleanup()
        try:
            idx = get_index()
            found, _ = idx.lookup([i["key"] for i in items])
        except SourceError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Drive")
            raise HTTPException(502, "No pude conectar con el Drive. Intenta de nuevo.") from exc

        missing = [i["code"] for i in items if i["key"] not in found]
        todo = [i for i in items if i["key"] in found]
        pdf_items: list[Item] = []
        errors: list[str] = []
        if todo:
            def work(i):
                try:
                    m = found[i["key"]]
                    jpeg, w, h = load_prepared(idx.source, m.photo)
                    return Item(m.code, i["note"], jpeg, w, h)
                except Exception:  # noqa: BLE001
                    log.exception("Foto %s", i["code"])
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                for i, res in zip(todo, pool.map(work, todo)):
                    if res is None:
                        errors.append(i["code"])
                    else:
                        pdf_items.append(res)

        base = {"missing": missing, "errors": errors}
        if not pdf_items:
            msg = "No encontré fotos para esos códigos." if not errors else "Encontré las fotos pero no pude leerlas."
            return {"ok": False, "message": msg, **base}

        per_page = req.per_page if req.per_page in LAYOUTS else 6
        title = req.title.strip() or TITLE
        now = dt.datetime.now(CO_TZ)
        pdf = build_pdf(
            pdf_items,
            title=title,
            per_page=per_page,
            show_code=req.show_code,
            brand_color=BRAND_COLOR,
            brand_name=BRAND_NAME,
            footer=FOOTER_TEXT,
            date_text=now.strftime("%d/%m/%Y"),
            logo_path=LOGO,
        )
        pid = secrets.token_hex(8)
        filename = f"{slug(title)}-{now.strftime('%Y%m%d')}.pdf"
        (PDF_DIR / f"{pid}.pdf").write_bytes(pdf)
        cols, rows = LAYOUTS[per_page]
        log.info("PDF %s: %d fotos, %d sin foto, %d KB", pid, len(pdf_items), len(missing), len(pdf) // 1024)
        return {
            "ok": True,
            "found": len(pdf_items),
            "pages": -(-len(pdf_items) // (cols * rows)),
            "size_kb": len(pdf) // 1024,
            "pdf_url": f"/pdf/{pid}/{filename}",
            "filename": filename,
            "expires_hours": PDF_TTL_HOURS,
            **base,
        }
    finally:
        _slots.release()


@app.get("/pdf/{pid}/{filename}")
def get_pdf(pid: str, filename: str, dl: int = 0):
    path = PDF_DIR / f"{pid}.pdf"
    if not re.fullmatch(r"[0-9a-f]{16}", pid) or not path.is_file():
        return JSONResponse({"error": "Este PDF ya no está disponible. Vuelve a generarlo."}, status_code=404)
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", filename)[:80] or "catalogo.pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=safe,
        content_disposition_type="attachment" if dl else "inline",
        headers={"X-Robots-Tag": "noindex"},
    )


# --------------------------------------------------------------------------- interfaz
@app.get("/logo.png")
def logo():
    if not LOGO.is_file():
        raise HTTPException(404)
    return FileResponse(LOGO, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})
