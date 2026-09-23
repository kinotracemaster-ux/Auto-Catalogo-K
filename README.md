# Auto Catálogo K 📦📄

Aplicación web rápida y moderna para generar catálogos de productos en PDF (formato A4) a partir de códigos de referencia y fotos alojadas en Google Drive o almacenamiento local.

---

## 🚀 ¿Cómo funciona la aplicación?

1. **Ingreso de Códigos**: El usuario pega una lista de códigos en la interfaz web (uno por línea, o separados por comas/espacios). También permite agregar una descripción o precio con el separador `|` (ejemplo: `892B-D2 | Cronógrafo negro $150.000`).
2. **Búsqueda Automática en Google Drive**: La app utiliza una cuenta de servicio de Google Cloud (o una clave de API) para buscar en la carpeta de Drive (y sus subcarpetas) las fotos cuyos nombres coincidan con los códigos ingresados (ejemplo: `892B-D2.jpg`).
3. **Optimización con Pillow**: Las fotos se redimensionan en segundo plano con algoritmo Lanczos y compresión JPEG optimizada, almacenándose en una caché temporal para que la generación sea instantánea.
4. **Construcción del PDF con ReportLab**: Se ensambla un PDF limpio en cuadrícula configurable (4, 6, 9 o 12 fotos por hoja), con el color de marca, título del catálogo, fecha, logo y pie de página.
5. **Reporte y Descarga**: La app informa de inmediato si hubo códigos sin foto y permite visualizar o descargar el archivo PDF generado.

> **Carpetas por código**: las fotos suelen estar en una carpeta con lo que va antes del guion (`2624-1.jpg` dentro de la carpeta `2624`). Si una foto recién subida todavía no aparece, la app la busca en el momento dentro de esa carpeta.

---

## 🛠️ Tecnologías

- **Backend**: Python 3.11+ / FastAPI / Uvicorn
- **Generación PDF**: ReportLab
- **Procesamiento de Imágenes**: Pillow
- **Almacenamiento en la Nube**: Google Drive API v3 (`google-auth`, `requests`)
- **Frontend**: HTML5, CSS moderno y JavaScript Vanilla (sin dependencias externas pesadas)

---

## ⚙️ Variables de Entorno

En Railway o en un archivo `.env` local, configura las siguientes variables:

### Obligatorias
| Variable | Descripción |
| :--- | :--- |
| `DRIVE_FOLDER_ID` | ID de la carpeta de Drive o su enlace completo (`https://drive.google.com/drive/folders/...`). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Contenido completo del archivo JSON de la cuenta de servicio de Google (o codificado en base64). Recuerda compartir la carpeta de Drive con el correo `client_email` de la cuenta de servicio con rol **Lector**. |
| `GOOGLE_API_KEY` | **Alternativa** a la cuenta de servicio, para cuando Google no deja crear la clave JSON (política `iam.disableServiceAccountKeyCreation`). Ver abajo. |

Usa **una** de las dos: `GOOGLE_SERVICE_ACCOUNT_JSON` o `GOOGLE_API_KEY`. Si pones ambas, se usa la cuenta de servicio.

#### Usar una clave de API en vez de la cuenta de servicio
1. En Google Cloud: **APIs y servicios → Credenciales → Crear credenciales → Clave de API**.
2. En la clave: **Restricciones de aplicaciones: Ninguna** (Railway no tiene IP fija) y **Restricciones de API: Google Drive API**.
3. En Drive, comparte la carpeta de fotos como **Cualquier persona con el enlace → Lector** (las subcarpetas y fotos lo heredan). Ojo: quien tenga el enlace de la carpeta puede ver las fotos.
4. Pon la clave en `GOOGLE_API_KEY` y deja vacía `GOOGLE_SERVICE_ACCOUNT_JSON`.

### Opcionales
| Variable | Por Defecto | Descripción |
| :--- | :--- | :--- |
| `ACCESS_CODE` | *(vacío)* | Clave de acceso obligatoria para que los usuarios puedan generar catálogos. |
| `APP_NAME` | `Generador de catálogo` | Nombre que se muestra en la cabecera de la web. |
| `CATALOG_TITLE`| `Catálogo` | Título predeterminado del PDF. |
| `BRAND_COLOR` | `#E41F29` | Color hexadecimal de la franja superior del catálogo y acentos de la web. |
| `BRAND_NAME` | *(vacío)* | Nombre de la marca o empresa en el pie de página. |
| `FOOTER_TEXT` | *(vacío)* | Texto en el pie de página del PDF (ej: *Pedidos WhatsApp +57 300 000 0000*). |
| `MAX_CODES` | `150` | Límite máximo de códigos por PDF. |
| `IMG_MAX_PX` | `900` | Resolución máxima de lado para las fotos en el PDF. |
| `JPEG_QUALITY` | `80` | Calidad de compresión JPEG (1-100). |
| `INDEX_TTL_SECONDS` | `300` | Tiempo en segundos de caché del listado de fotos de Drive. |
| `PDF_TTL_HOURS` | `24` | Horas antes de eliminar los PDFs temporales generados. |

---

## 💻 Ejecución en Local

1. Crea y activa un entorno virtual:
   ```bash
   py -m venv .venv
   .venv\Scripts\activate   # En Windows
   ```
2. Instala las dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Crea un archivo `.env` tomando como base `.env.example` y define tus credenciales.
4. Inicia el servidor de desarrollo:
   ```bash
   uvicorn app:app --reload
   ```
5. Abre en tu navegador: [http://localhost:8000](http://localhost:8000)

---

## 📤 Paso a Paso: Subir a GitHub

1. Abre la terminal en esta carpeta (`Auto Catalgo K`).
2. Verifica el estado de git:
   ```bash
   git status
   ```
3. Agrega todos los archivos al commit:
   ```bash
   git add .
   git commit -m "Initial commit: Auto Catalogo K app"
   ```
4. Crea un nuevo repositorio en tu cuenta de GitHub (por ejemplo: `Auto-Catalogo-K`).
5. Vincula el repositorio remoto y sube tu código:
   ```bash
   git remote add origin https://github.com/kinotracemaster-ux/Auto-Catalogo-K.git
   git branch -M main
   git push -u origin main
   ```

---

## 🚆 Paso a Paso: Desplegar en Railway

1. Entra a [railway.com](https://railway.com) e inicia sesión con tu cuenta de GitHub.
2. Haz clic en **New Project** > **Deploy from GitHub repo**.
3. Selecciona el repositorio `Auto-Catalogo-K`.
4. Railway detectará automáticamente el archivo `railway.json`, `Procfile` y `requirements.txt`.
5. Ve a la pestaña **Variables** del servicio en Railway y agrega las variables:
   - `DRIVE_FOLDER_ID`: El ID o URL de tu carpeta de Google Drive con las fotos.
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: El texto completo del archivo JSON de tu cuenta de servicio de Google (o, en su lugar, `GOOGLE_API_KEY`).
   - *(Opcionales)*: `ACCESS_CODE`, `BRAND_COLOR`, `FOOTER_TEXT`, etc.
6. Ve a la pestaña **Settings** > **Networking** y haz clic en **Generate Domain** para obtener tu enlace público (ejemplo: `https://auto-catalogo-k-production.up.railway.app`).
7. ¡Listo! Abre la URL pública y genera tus catálogos.
