"""Fuentes de fotos para el catalogo.

- DriveSource: lee una carpeta de Google Drive (y sus subcarpetas) con una cuenta de servicio,
  o con una clave de API si la carpeta esta compartida como "Cualquier persona con el enlace".
  Pensada para Drives enormes (decenas de miles de carpetas): no lista todo, solo los primeros
  niveles, y cada codigo se busca dentro de su carpeta (839B-6 -> 839B/PRINCIPAL/839B-6.png).
- LocalSource: lee una carpeta del disco (pruebas, o Drive de escritorio).

Regla de busqueda: el nombre del archivo (sin extension) es el codigo del producto.
Tambien se aceptan fotos extra con sufijo _1, _2 ... (ej. 892B-D2_1.jpg). Si no existe
la foto principal, se usa la primera de esas.
Las fotos suelen estar en una carpeta con lo que va antes del guion (2624-1 -> carpeta 2624),
directamente o en subcarpetas (PRINCIPAL, SECUNDARIAS...).
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
    # formatos que se pueden poner en el PDF (no .psd, .ai, heic ni videos)
    IMG_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"}
    INDEX_DEPTH = 4  # niveles que se listan al arrancar: PRODUCTOS/marca/HOMBRE/839B
    CODE_DEPTH = 3  # niveles que se revisan dentro de la carpeta de un codigo: 839B/PRINCIPAL/...
    MAX_FOLDERS = 20000
    # Drive responde rapido una carpeta por consulta (~0.2 s) y lento si se piden varias juntas:
    # por eso se pregunta carpeta por carpeta, muchas a la vez.
    WORKERS = 16

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
        if hasattr(session, "mount"):  # que las consultas en paralelo reusen conexiones
            from requests.adapters import HTTPAdapter

            session.mount("https://", HTTPAdapter(pool_maxsize=2 * self.WORKERS))
        self.session = session
        self.email = info.get("client_email", "")
        self.folder_count = 0
        self._folders_by_name: dict[str, list[str]] = {}  # nombre de carpeta -> ids (2624 -> [...])

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

    def _children(self, folder: str) -> list[dict]:
        """Todo lo que hay dentro de una carpeta (todas las paginas). Una subcarpeta que no se puede leer se salta."""
        params = {
            "q": f"'{folder}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        files: list[dict] = []
        while True:
            r = self._get(self.API, params)
            if r.status_code != 200:
                limited = "ratelimitexceeded" in (r.text or "").lower()
                if r.status_code in (403, 404) and folder != self.folder_id and not limited \
                        and not self._key_problem(self._detail(r)):
                    log.warning("Salto la subcarpeta %s: Drive no la deja leer (%s)", folder, r.status_code)
                    return files
                raise self._error(r, folder)
            data = r.json()
            files.extend(data.get("files", []))
            if not data.get("nextPageToken"):
                return files
            params["pageToken"] = data["nextPageToken"]

    def _walk(self, roots: list[str], depth: int) -> tuple[list[tuple[str, Photo]], list[dict], int]:
        """Recorre esas carpetas y sus subcarpetas hasta `depth` niveles (las mas cercanas primero),
        una consulta por carpeta y varias a la vez.
        Devuelve ([(carpeta de arranque, foto)], [subcarpetas vistas], cuantas carpetas se leyeron)."""
        photos: list[tuple[str, Photo]] = []
        folders: list[dict] = []
        origin = {r: r for r in roots}
        level, seen = list(dict.fromkeys(roots)), set()
        with ThreadPoolExecutor(self.WORKERS) as pool:
            for _ in range(depth):
                level = [f for f in level if f not in seen][: max(0, self.MAX_FOLDERS - len(seen))]
                if not level:
                    break
                seen.update(level)
                nxt = []
                for parent, files in zip(level, pool.map(self._children, level)):
                    for f in files:
                        mime = f.get("mimeType", "")
                        if mime == self.FOLDER_MIME:
                            origin.setdefault(f["id"], origin[parent])
                            nxt.append(f["id"])
                            folders.append(f)
                        elif mime in self.IMG_MIMES:
                            photos.append((origin[parent], self._photo(f)))
                level = nxt
        if len(seen) >= self.MAX_FOLDERS:
            log.warning("Hay mas de %d carpetas: las demas no se revisan", self.MAX_FOLDERS)
        return photos, folders, len(seen)

    def list_photos(self) -> list[Photo]:
        """Lista los primeros niveles: los nombres de las carpetas (para ubicar la de cada codigo)
        y las fotos que haya en ellos. Las fotos mas adentro se buscan con photos_under."""
        photos, folders, count = self._walk([self.folder_id], self.INDEX_DEPTH)
        by_name: dict[str, list[str]] = {}
        for f in folders:
            by_name.setdefault(norm_key(f["name"]), []).append(f["id"])
        self.folder_count = count
        self._folders_by_name = by_name
        return [p for _, p in photos]

    @staticmethod
    def _photo(f: dict) -> Photo:
        return Photo(f["id"], f["name"], f.get("md5Checksum") or f.get("modifiedTime", ""))

    def folders_named(self, name: str) -> list[str]:
        """Ids de las carpetas con ese nombre (ya normalizado), segun el ultimo listado."""
        return self._folders_by_name.get(name, [])

    def photos_under(self, folders: list[str]) -> dict[str, list[Photo]]:
        """Fotos dentro de esas carpetas y sus subcarpetas (PRINCIPAL, SECUNDARIAS...), preguntando ya mismo."""
        photos, _, _ = self._walk(folders, self.CODE_DEPTH)
        out: dict[str, list[Photo]] = {f: [] for f in folders}
        for root, photo in photos:
            out[root].append(photo)
        return out

    # Estos enlaces le sirven a cualquiera si la carpeta es publica con enlace
    @staticmethod
    def view_link(photo: Photo) -> str:
        """Abre la foto en Drive."""
        return f"https://drive.google.com/file/d/{photo.ref}/view"

    @staticmethod
    def download_link(photo: Photo) -> str:
        """Descarga la foto original."""
        return f"https://drive.google.com/uc?id={photo.ref}&export=download"

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

    # las fotos del disco no tienen enlace
    @staticmethod
    def view_link(photo: Photo) -> str:
        return ""

    @staticmethod
    def download_link(photo: Photo) -> str:
        return ""

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
    """Busca las fotos de los codigos.

    1. Mapa en memoria de las carpetas y fotos de los primeros niveles (`source.list_photos`).
       Solo la primera carga hace esperar; despues se refresca en segundo plano cada `ttl` segundos
       (o si piden un codigo cuya carpeta no aparece), maximo una vez cada `min_refresh` segundos.
    2. Si un codigo no esta en el mapa, entra a la carpeta del codigo (839B-6 -> 839B) y a sus
       subcarpetas y busca ahi el nombre exacto. Lo que encuentra se recuerda `folder_ttl` segundos.
    """

    def __init__(self, source, ttl: int = 1800, min_refresh: int = 120, folder_ttl: int = 120):
        self.source = source
        self.ttl = ttl
        self.min_refresh = min_refresh
        self.folder_ttl = folder_ttl
        self._lock = threading.Lock()
        self._map: dict[str, list[tuple[int, Photo, str]]] = {}
        self._photo_count = 0
        self._loaded_at = 0.0
        self._last_attempt = 0.0
        self._refreshing = False
        self._folder_cache: dict[str, tuple[float, list[Photo]]] = {}

    def _build(self) -> tuple[dict[str, list[tuple[int, Photo, str]]], int]:
        started = time.time()
        photos = self.source.list_photos()
        mapping = self._mapping(photos)
        log.info("Indice de fotos listo: %d fotos en %.1f s", len(photos), time.time() - started)
        return mapping, len(photos)

    @staticmethod
    def _mapping(photos: list[Photo]) -> dict[str, list[tuple[int, Photo, str]]]:
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
        return mapping

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

        found = self._find(self.get(), keys)
        missing = [k for k in keys if k not in found]
        if missing:  # quiza acaban de subir la foto: la busca ya mismo en su carpeta (2624-1 -> 2624)
            found.update(self._from_folders(missing))
            missing = [k for k in keys if k not in found]
        if missing:  # carpeta nueva o nombre distinto: relista todo en segundo plano para el proximo intento
            self.get(force=True)
        return found, missing

    @staticmethod
    def _find(data: dict[str, list[tuple[int, Photo, str]]], keys: list[str]) -> dict[str, Match]:
        return {k: Match(data[k][0][1], data[k][0][2]) for k in keys if k in data}

    def _code_folders(self, key: str) -> list[str]:
        """Carpetas del codigo: la de nombre mas largo que calce (S-696-1 -> S-696 antes que S; 839B-6 -> 839B)."""
        parts = key.split("-")
        for n in range(len(parts), 0, -1):
            ids = self.source.folders_named("-".join(parts[:n]))
            if ids:
                return ids
        return []

    def _from_folders(self, keys: list[str]) -> dict[str, Match]:
        if not hasattr(self.source, "photos_under"):
            return {}
        folders = list(dict.fromkeys(f for k in keys for f in self._code_folders(k)))
        if not folders:
            return {}
        now = time.time()
        with self._lock:
            cached = {f: c[1] for f in folders if (c := self._folder_cache.get(f)) and now - c[0] < self.folder_ttl}
        todo = [f for f in folders if f not in cached]
        if todo:
            try:
                fresh = self.source.photos_under(todo)
            except Exception:  # noqa: BLE001
                log.warning("No pude revisar las carpetas de %s", keys[:10], exc_info=True)
                fresh = {}
            with self._lock:
                for f, photos in fresh.items():
                    self._folder_cache[f] = (now, photos)
            cached.update(fresh)
        return self._find(self._mapping([p for f in folders for p in cached.get(f, [])]), keys)

    @property
    def photo_count(self) -> int:
        return self._photo_count

    def sample(self, n: int = 5) -> list[str]:
        return [lst[0][1].name for lst in list(self._map.values())[:n]]
