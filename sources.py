"""Fuentes de fotos para el catalogo.

- DriveSource: lee una carpeta de Google Drive (y sus subcarpetas) con una cuenta de servicio,
  o con una clave de API si la carpeta esta compartida como "Cualquier persona con el enlace".
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
from concurrent.futures import ThreadPoolExecutor
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
    value = (value or "").strip()
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
    raw = (raw or "").strip()
    if not raw:
        path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if path and Path(path).is_file():
            return json.loads(Path(path).read_text(), strict=False)
        raise SourceError(
            "Falta GOOGLE_SERVICE_ACCOUNT_JSON (el JSON de la cuenta de servicio) o GOOGLE_API_KEY (clave de API)."
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
    FOLDER_MIME = "application/vnd.google-apps.folder"
    MAX_FOLDERS = 5000
    PARENTS_PER_QUERY = 30  # carpetas por consulta: ('a' in parents or 'b' in parents ...)
    WORKERS = 8  # consultas a Drive al mismo tiempo

    def __init__(self, folder_id: str, credentials_json: str | None = None, session=None, api_key: str | None = None):
        self.folder_id = extract_folder_id(folder_id)
        self.api_key = (api_key or "").strip()
        info: dict = {}
        if session is None and self.api_key:
            import requests

            session = requests.Session()
        elif session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            info = load_service_account(credentials_json)
            creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            session = AuthorizedSession(creds)
        self.session = session
        self.email = info.get("client_email", "")
        self.folder_count = 0

    # -- http con reintentos
    def _get(self, url: str, params: dict | None = None, timeout: int = 45):
        import requests

        if self.api_key:
            params = {**(params or {}), "key": self.api_key}
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
                continue
            limited = r.status_code == 403 and "ratelimitexceeded" in (r.text or "").lower()
            if (limited or r.status_code in (429, 500, 502, 503, 504)) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return r
        return r  # pragma: no cover

    @staticmethod
    def _detail(r) -> str:
        try:
            return r.json().get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            return (r.text or "")[:200]

    def _key_problem(self, detail: str) -> bool:
        return bool(self.api_key) and any(
            k in detail.lower() for k in ("api key", "blocked", "has not been used", "is disabled")
        )

    def _error(self, r, folder: str) -> SourceError:
        detail = self._detail(r)
        if self._key_problem(detail):
            return SourceError(
                "Google rechazo GOOGLE_API_KEY. Revisa que este bien copiada, que tenga habilitada "
                f"Google Drive API y sin restriccion de sitios web ni de IP. ({detail[:150]})"
            )
        if r.status_code in (403, 404) and folder == self.folder_id and self.api_key:
            return SourceError(
                "No encuentro la carpeta de Drive. Revisa DRIVE_FOLDER_ID y que este compartida como "
                '"Cualquier persona con el enlace" (Lector).'
            )
        if r.status_code == 404 and folder == self.folder_id:
            who = f" con {self.email}" if self.email else " con la cuenta de servicio"
            return SourceError(
                f"No encuentro la carpeta de Drive. Revisa DRIVE_FOLDER_ID y que este compartida{who} (permiso Lector)."
            )
        return SourceError(f"Drive respondio {r.status_code}: {detail[:200]}")

    def _children(self, folders: list[str]) -> list[dict]:
        """Todo lo que hay dentro de varias carpetas, con una sola consulta (todas las paginas).
        Si Drive rechaza la consulta, la parte en dos; una subcarpeta que no se puede leer se salta."""
        parents = " or ".join(f"'{f}' in parents" for f in folders)
        params = {
            "q": f"({parents}) and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        files: list[dict] = []
        while True:
            r = self._get(self.API, params)
            if r.status_code != 200:
                first_page = "pageToken" not in params
                if first_page and len(folders) > 1 and r.status_code >= 400 and not self._key_problem(self._detail(r)):
                    mid = len(folders) // 2
                    return self._children(folders[:mid]) + self._children(folders[mid:])
                limited = "ratelimitexceeded" in (r.text or "").lower()
                if r.status_code in (403, 404) and folders[0] != self.folder_id and not limited:
                    log.warning("Salto la subcarpeta %s: Drive no la deja leer (%s)", folders[0], r.status_code)
                    return []
                raise self._error(r, folders[0])
            data = r.json()
            files.extend(data.get("files", []))
            if not data.get("nextPageToken"):
                return files
            params["pageToken"] = data["nextPageToken"]

    def list_photos(self) -> list[Photo]:
        """Recorre la carpeta y sus subcarpetas por niveles (las mas cercanas a la raiz primero).
        Cada consulta pide muchas carpetas juntas y se hacen varias consultas a la vez:
        con cientos de subcarpetas son unos pocos segundos en vez de minutos."""
        photos: list[Photo] = []
        seen: set[str] = set()
        level = [self.folder_id]
        with ThreadPoolExecutor(self.WORKERS) as pool:
            while level and len(seen) < self.MAX_FOLDERS:
                level = [f for f in dict.fromkeys(level) if f not in seen][: self.MAX_FOLDERS - len(seen)]
                seen.update(level)
                n = self.PARENTS_PER_QUERY
                chunks = [level[i : i + n] for i in range(0, len(level), n)]
                level = []
                for files in pool.map(self._children, chunks):
                    for f in files:
                        mime = f.get("mimeType", "")
                        if mime == self.FOLDER_MIME:
                            level.append(f["id"])
                        elif mime.startswith("image/"):
                            photos.append(Photo(f["id"], f["name"], f.get("md5Checksum") or f.get("modifiedTime", "")))
        if level:
            log.warning("Hay mas de %d carpetas: las demas no se revisan", self.MAX_FOLDERS)
        self.folder_count = len(seen)
        return photos

    def fetch(self, photo: Photo) -> bytes:
        r = self._get(f"{self.API}/{photo.ref}", {"alt": "media", "supportsAllDrives": "true"}, timeout=90)
        if r.status_code != 200:
            raise SourceError(f"Drive respondio {r.status_code} al bajar {photo.name}")
        return r.content

    def describe(self) -> dict:
        out = {"kind": self.kind, "folder": self.folder_id, "folders_read": self.folder_count}
        if self.api_key:
            return {**out, "auth": "api_key"}
        return {**out, "service_account": self.email}


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


def make_source_from_env():
    folder = os.getenv("DRIVE_FOLDER_ID", "").strip()
    local = os.getenv("LOCAL_IMAGES_DIR", "").strip()
    if folder:
        credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        api_key = os.getenv("GOOGLE_API_KEY", "").strip()
        if api_key and not credentials and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            return DriveSource(folder, api_key=api_key)
        return DriveSource(folder, credentials)
    if local:
        return LocalSource(local)
    raise SourceError("Falta configurar DRIVE_FOLDER_ID (la carpeta de Drive con las fotos).")


# --------------------------------------------------------------------------- Indice
class PhotoIndex:
    """Mapa codigo -> fotos, en memoria: buscar un codigo es instantaneo, sin preguntarle al Drive.

    Solo la primera carga hace esperar. Despues, cuando el mapa tiene mas de `ttl` segundos
    (o piden un codigo que no aparece, por si acaban de subir la foto), se vuelve a listar
    en segundo plano sin frenar a nadie: maximo una vez cada `min_refresh` segundos.
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
        self._refreshing = False

    def _build(self) -> tuple[dict[str, list[tuple[int, Photo, str]]], int]:
        started = time.time()
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
        log.info("Indice de fotos listo: %d fotos en %.1f s", len(photos), time.time() - started)
        return mapping, len(photos)

    def _refresh(self) -> None:
        """Relista en segundo plano; si falla, sigue con el mapa anterior."""
        try:
            mapping, count = self._build()
            with self._lock:
                self._map, self._photo_count, self._loaded_at = mapping, count, time.time()
        except Exception:  # noqa: BLE001
            log.exception("No pude refrescar el indice; sigo con el anterior")
        finally:
            self._refreshing = False

    def get(self, force: bool = False) -> dict[str, list[tuple[int, Photo, str]]]:
        with self._lock:
            now = time.time()
            due = force or now - self._loaded_at > self.ttl
            if not self._loaded_at:  # primera carga: aqui si hay que esperar
                self._last_attempt = now
                self._map, self._photo_count = self._build()
                self._loaded_at = time.time()
            elif due and not self._refreshing and now - self._last_attempt > self.min_refresh:
                self._refreshing = True
                self._last_attempt = now
                threading.Thread(target=self._refresh, name="refrescar-indice", daemon=True).start()
            return self._map

    def lookup(self, keys: list[str]) -> tuple[dict[str, Match], list[str]]:
        """Devuelve ({clave: Match(foto, codigo del Drive)}, [claves sin foto])."""

        data = self.get()
        found = {k: Match(data[k][0][1], data[k][0][2]) for k in keys if k in data}
        missing = [k for k in keys if k not in found]
        if missing:  # quiza acaban de subir la foto: relista en segundo plano para el proximo intento
            self.get(force=True)
        return found, missing

    @property
    def photo_count(self) -> int:
        return self._photo_count

    def sample(self, n: int = 5) -> list[str]:
        return [lst[0][1].name for lst in list(self._map.values())[:n]]
