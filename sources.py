"""Fuentes de fotos para el catalogo.

- DriveSource: lee una carpeta de Google Drive (y sus subcarpetas) con una cuenta de servicio.
- LocalSource: lee una carpeta del disco (pruebas, o Drive de escritorio).

Regla de busqueda: el nombre del archivo (sin extension) es el codigo del producto.
Tambien se aceptan fotos extra con sufijo _1, _2 ... (ej. 892B-D2_1.jpg). Si no existe
la foto principal, se usa la primera de esas.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("catalogo.sources")

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class SourceError(Exception):
    """Error con mensaje entendible para mostrarle al usuario."""


def norm_key(text: str) -> str:
    """Normaliza un codigo o nombre de archivo para compararlos:
    mayusculas, sin espacios y con los guiones raros unificados."""
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"\s+", "", s)
    return s.upper()


def extract_folder_id(value: str) -> str:
    """Acepta el ID de la carpeta o el enlace completo de Drive."""
    value = (value or "").strip().strip("\"'").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


@dataclass(frozen=True)
class Photo:
    ref: str      # id de Drive o ruta local
    name: str     # nombre de archivo original
    version: str  # cambia si la foto cambia (md5 / mtime): sirve para la cache

    @property
    def cache_id(self) -> str:
        return hashlib.sha1(f"{self.ref}|{self.version}".encode()).hexdigest()


class Match(NamedTuple):
    photo: Photo
    code: str  # el codigo como esta escrito en el nombre del archivo


# --------------------------------------------------------------------------- Drive
def load_service_account(raw: str | None) -> dict:
    """JSON de la cuenta de servicio: texto JSON, base64 de ese JSON, o ruta por GOOGLE_APPLICATION_CREDENTIALS."""
    raw = (raw or "").strip().strip("\"'").strip()
    if not raw:
        path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if path and Path(path).is_file():
            return json.loads(Path(path).read_text(), strict=False)
        raise SourceError(
            "Falta GOOGLE_SERVICE_ACCOUNT_JSON (el JSON de la cuenta de servicio). "
            "En Railway: servicio > Variables > New Variable, nombre GOOGLE_SERVICE_ACCOUNT_JSON y como valor "
            "el contenido completo del archivo .json de la cuenta de servicio; luego pulsa Deploy."
        )
    try:
        if raw.startswith("{"):
            return json.loads(raw, strict=False)
        return json.loads(base64.b64decode(raw).decode(), strict=False)
    except Exception as exc:  # noqa: BLE001
        raise SourceError("GOOGLE_SERVICE_ACCOUNT_JSON no se pudo leer: pega el JSON completo (o su base64).") from exc


class DriveSource:
    kind = "drive"
    API = "https://www.googleapis.com/drive/v3/files"
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    MAX_FOLDERS = 500

    def __init__(self, folder_id: str, credentials_json: str | None = None, session=None):
        self.folder_id = extract_folder_id(folder_id)
        info: dict = {}
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            info = load_service_account(credentials_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            session = AuthorizedSession(creds)
        self.session = session
        self.email = info.get("client_email", "")

    # -- http con reintentos
    def _get(self, url: str, params: dict | None = None, timeout: int = 45):
        import requests

        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return r
        return r  # pragma: no cover

    def _error(self, r, folder: str) -> SourceError:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = (r.text or "")[:200]
        if r.status_code == 404 and folder == self.folder_id:
            who = f" con {self.email}" if self.email else " con la cuenta de servicio"
            return SourceError(
                f"No encuentro la carpeta de Drive. Revisa DRIVE_FOLDER_ID y que este compartida{who} (permiso Lector)."
            )
        return SourceError(f"Drive respondio {r.status_code}: {detail[:200]}")

    def list_photos(self) -> list[Photo]:
        """Recorre la carpeta y sus subcarpetas (las mas cercanas a la raiz primero)."""
        photos: list[Photo] = []
        queue, seen = [self.folder_id], set()
        while queue and len(seen) < self.MAX_FOLDERS:
            folder = queue.pop(0)
            if folder in seen:
                continue
            seen.add(folder)
            token = None
            while True:
                params = {
                    "q": f"'{folder}' in parents and trashed = false",
                    "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime)",
                    "pageSize": 1000,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if token:
                    params["pageToken"] = token
                r = self._get(self.API, params)
                if r.status_code != 200:
                    raise self._error(r, folder)
                data = r.json()
                for f in data.get("files", []):
                    mime = f.get("mimeType", "")
                    if mime == "application/vnd.google-apps.folder":
                        queue.append(f["id"])
                    elif mime.startswith("image/"):
                        photos.append(Photo(f["id"], f["name"], f.get("md5Checksum") or f.get("modifiedTime", "")))
                token = data.get("nextPageToken")
                if not token:
                    break
        return photos

    def fetch(self, photo: Photo) -> bytes:
        r = self._get(f"{self.API}/{photo.ref}", {"alt": "media", "supportsAllDrives": "true"}, timeout=90)
        if r.status_code != 200:
            raise SourceError(f"Drive respondio {r.status_code} al bajar {photo.name}")
        return r.content

    def describe(self) -> dict:
        return {"kind": self.kind, "folder": self.folder_id, "service_account": self.email}


# --------------------------------------------------------------------------- Local
class LocalSource:
    kind = "local"

    def __init__(self, root: str):
        self.root = Path(root)

    def list_photos(self) -> list[Photo]:
        if not self.root.is_dir():
            raise SourceError(f"No existe la carpeta de fotos: {self.root}")
        files = [p for p in self.root.rglob("*") if p.is_file() and p.suffix.lower() in IMG_EXT and not p.name.startswith(".")]
        files.sort(key=lambda p: (len(p.relative_to(self.root).parts), str(p)))
        out = []
        for p in files:
            st = p.stat()
            out.append(Photo(str(p), p.name, f"{st.st_mtime_ns}-{st.st_size}"))
        return out

    def fetch(self, photo: Photo) -> bytes:
        return Path(photo.ref).read_bytes()

    def describe(self) -> dict:
        return {"kind": self.kind, "folder": str(self.root)}


FOLDER_VARS = ("DRIVE_FOLDER_ID", "DRIVE_FOLDER", "DRIVE_FOLDER_URL", "GOOGLE_DRIVE_FOLDER_ID")
CREDENTIAL_VARS = ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT", "GOOGLE_CREDENTIALS_JSON", "GOOGLE_CREDENTIALS")


def env_value(names: tuple[str, ...]) -> str:
    """Primer valor no vacio entre esos nombres de variable, sin importar mayusculas,
    guiones o espacios de mas en el nombre (en Railway es facil escribir 'drive_folder_id')."""

    def simple(name: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", name.upper())

    found = {simple(k): v.strip() for k, v in os.environ.items() if v.strip()}
    for name in names:
        if found.get(simple(name)):
            return found[simple(name)]
    return ""


def make_source_from_env():
    folder = env_value(FOLDER_VARS)
    local = os.getenv("LOCAL_IMAGES_DIR", "").strip()
    if folder:
        return DriveSource(folder, env_value(CREDENTIAL_VARS))
    if local:
        return LocalSource(local)
    raise SourceError(
        "Falta configurar DRIVE_FOLDER_ID (la carpeta de Drive con las fotos). "
        "En Railway: servicio > Variables > New Variable, nombre DRIVE_FOLDER_ID y como valor "
        "el enlace de la carpeta; luego pulsa Deploy para aplicar el cambio."
    )


# --------------------------------------------------------------------------- Indice
class PhotoIndex:
    """Mapa codigo -> fotos, en memoria, para no listar el Drive en cada pedido.

    Se refresca solo cada `ttl` segundos y, si piden un codigo que no aparece,
    se vuelve a listar (maximo una vez cada `min_refresh` segundos) por si acaban de subir la foto.
    """

    def __init__(self, source, ttl: int = 300, min_refresh: int = 20):
        self.source = source
        self.ttl = ttl
        self.min_refresh = min_refresh
        self._lock = threading.Lock()
        self._map: dict[str, list[tuple[int, Photo, str]]] = {}
        self._photo_count = 0
        self._loaded_at = 0.0
        self._last_attempt = 0.0

    def _build(self) -> dict[str, list[tuple[int, Photo, str]]]:
        photos = self.source.list_photos()
        mapping: dict[str, list[tuple[int, Photo, str]]] = {}
        for p in photos:
            stem = p.name.rsplit(".", 1)[0] if "." in p.name else p.name
            # (prioridad, foto, codigo tal como se escribe en el Drive)
            mapping.setdefault(norm_key(stem), []).append((0, p, stem))
            m = re.match(r"^(.+?)_(\d+)$", stem)
            if m:
                mapping.setdefault(norm_key(m.group(1)), []).append((1 + int(m.group(2)), p, m.group(1)))
        for lst in mapping.values():
            lst.sort(key=lambda t: (t[0], t[1].name))
        self._photo_count = len(photos)
        return mapping

    def get(self, force: bool = False) -> dict[str, list[tuple[int, Photo, str]]]:
        with self._lock:
            now = time.time()
            due = (not self._loaded_at) or (now - self._loaded_at > self.ttl)
            if force:
                due = now - self._last_attempt > self.min_refresh
            elif due and self._map and now - self._last_attempt < 30:
                due = False  # fallo hace poco: no martillar el Drive
            if due:
                self._last_attempt = now
                try:
                    self._map = self._build()
                    self._loaded_at = time.time()
                except Exception:
                    if not self._map:
                        raise
                    log.exception("No pude refrescar el indice; sigo con el anterior")
            return self._map

    def lookup(self, keys: list[str]) -> tuple[dict[str, Match], list[str]]:
        """Devuelve ({clave: Match(foto, codigo del Drive)}, [claves sin foto])."""

        def find(data):
            return {k: Match(data[k][0][1], data[k][0][2]) for k in keys if k in data}

        found = find(self.get())
        missing = [k for k in keys if k not in found]
        if missing:  # quiza acaban de subir la foto: relista (con limite de frecuencia)
            found = find(self.get(force=True))
            missing = [k for k in keys if k not in found]
        return found, missing

    @property
    def photo_count(self) -> int:
        return self._photo_count

    def sample(self, n: int = 5) -> list[str]:
        return [lst[0][1].name for lst in list(self._map.values())[:n]]
