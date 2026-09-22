"""Arma el PDF del catalogo (A4 vertical) con reportlab y prepara las fotos (Pillow)."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

# fotos por hoja -> (columnas, filas)
LAYOUTS = {4: (2, 2), 6: (2, 3), 9: (3, 3), 12: (3, 4)}

INK = HexColor("#18181B")
MUTED = HexColor("#52525B")
SOFT = HexColor("#71717A")
BORDER = HexColor("#E4E4E7")
PANEL = HexColor("#F6F6F7")


@dataclass
class Item:
    code: str
    note: str
    jpeg: bytes
    width: int
    height: int


def latin(text: str) -> str:
    """Las fuentes estandar del PDF solo entienden Latin-1/cp1252 (tildes y n si; emojis no)."""
    return (text or "").encode("cp1252", "ignore").decode("cp1252")


def prepare_image(raw: bytes, max_px: int = 900, quality: int = 80) -> tuple[bytes, int, int]:
    """Reduce la foto (los originales pesan varios MB) y la deja como JPEG RGB."""
    with Image.open(BytesIO(raw)) as im:
        im.draft("RGB", (max_px * 2, max_px * 2))  # JPEG grandes: decodifica mas rapido
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        out = BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue(), im.width, im.height


def _fit(text: str, font: str, size: float, max_w: float, min_size: float = 7) -> tuple[str, float]:
    """Achica la letra (y si hace falta recorta con ...) para que el texto quepa."""
    while size > min_size and stringWidth(text, font, size) > max_w:
        size -= 0.5
    if stringWidth(text, font, size) > max_w:
        while text and stringWidth(text + "…", font, size) > max_w:
            text = text[:-1]
        text += "…"
    return text, size


def _lines(text: str, font: str, size: float, max_w: float, max_lines: int) -> list[str]:
    lines = simpleSplit(text, font, size, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and stringWidth(last + "…", font, size) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def build_pdf(
    items: list[Item],
    title: str = "Catálogo",
    per_page: int = 6,
    show_code: bool = True,
    brand_color: str = "#E41F29",
    brand_name: str = "",
    footer: str = "",
    date_text: str = "",
    logo_path: str | Path | None = None,
) -> bytes:
    if not items:
        raise ValueError("No hay fotos para el PDF")
    cols, rows = LAYOUTS.get(per_page, LAYOUTS[6])
    per_page = cols * rows
    total_pages = -(-len(items) // per_page)

    W, H = A4
    margin, gap, pad = 12 * mm, 5 * mm, 3 * mm
    header_h, footer_h = 17 * mm, 11 * mm
    top = H - header_h - 7 * mm
    bottom = footer_h + 4 * mm
    cell_w = (W - 2 * margin - (cols - 1) * gap) / cols
    cell_h = (top - bottom - (rows - 1) * gap) / rows

    compact = cols >= 3
    code_size = 10 if compact else 12
    note_size = 8 if compact else 9
    text_w = cell_w - 2 * pad

    brand = HexColor(brand_color)
    title_txt = latin(title) or "Catálogo"
    logo = None
    if logo_path and Path(logo_path).is_file():
        try:
            logo = ImageReader(str(logo_path))
        except Exception:  # noqa: BLE001
            logo = None

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(title_txt)
    c.setAuthor(latin(brand_name) or "Catálogo")
    c.setCreator("Generador de catálogo")

    def chrome(page_no: int) -> None:
        c.setFillColor(brand)
        c.rect(0, H - header_h, W, header_h, stroke=0, fill=1)
        x = margin
        if logo is not None:
            lw, lh = logo.getSize()
            h = 9 * mm
            w = h * lw / lh
            c.drawImage(logo, x, H - header_h / 2 - h / 2, w, h, mask="auto")
            x += w + 4 * mm
        right_w = stringWidth(date_text, "Helvetica", 9) + 6 * mm if date_text else 0
        text, size = _fit(title_txt, "Helvetica-Bold", 17, W - margin - right_w - x, min_size=10)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", size)
        c.drawString(x, H - header_h / 2 - size * 0.35, text)
        if date_text:
            c.setFont("Helvetica", 9)
            c.drawRightString(W - margin, H - header_h / 2 - 3, date_text)
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.6)
        c.line(margin, footer_h, W - margin, footer_h)
        c.setFillColor(SOFT)
        c.setFont("Helvetica", 8)
        c.drawString(margin, footer_h - 10, latin(footer or brand_name))
        c.drawRightString(W - margin, footer_h - 10, f"Página {page_no} de {total_pages}")

    for pno, start in enumerate(range(0, len(items), per_page), 1):
        page_items = items[start:start + per_page]
        if pno > 1:
            c.showPage()
        chrome(pno)

        # El espacio de texto se ajusta a lo que hay en ESTA hoja (sin notas, las fotos quedan mas grandes)
        notes = {id(it): _lines(latin(it.note), "Helvetica", note_size, text_w, 2) if it.note else [] for it in page_items}
        n_lines = max(len(v) for v in notes.values())
        label_h = pad
        if show_code:
            label_h += code_size * 1.5
        if n_lines:
            label_h += note_size * 1.25 * n_lines + 2

        for pos, it in enumerate(page_items):
            r, col = divmod(pos, cols)
            x = margin + col * (cell_w + gap)
            y = top - r * (cell_h + gap) - cell_h

            c.setFillColor(white)
            c.setStrokeColor(BORDER)
            c.setLineWidth(0.7)
            c.roundRect(x, y, cell_w, cell_h, 3 * mm, stroke=1, fill=1)

            ax, ay = x + pad, y + label_h
            aw, ah = cell_w - 2 * pad, cell_h - label_h - pad
            c.setFillColor(PANEL)
            c.roundRect(ax, ay, aw, ah, 2 * mm, stroke=0, fill=1)
            scale = min(aw / it.width, ah / it.height)
            dw, dh = it.width * scale, it.height * scale
            c.drawImage(ImageReader(BytesIO(it.jpeg)), ax + (aw - dw) / 2, ay + (ah - dh) / 2, dw, dh)

            cx = x + cell_w / 2
            base = ay - 3
            if show_code:
                base -= code_size
                text, size = _fit(latin(it.code), "Helvetica-Bold", code_size, text_w)
                c.setFillColor(INK)
                c.setFont("Helvetica-Bold", size)
                c.drawCentredString(cx, base, text)
                base -= 3
            if notes[id(it)]:
                c.setFillColor(MUTED)
                c.setFont("Helvetica", note_size)
                for line in notes[id(it)]:
                    base -= note_size * 1.2
                    c.drawCentredString(cx, base, line)

    c.save()
    return buf.getvalue()
