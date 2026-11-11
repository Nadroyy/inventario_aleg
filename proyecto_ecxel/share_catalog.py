"""
Comparte catálogo como Google Spreadsheet nativo (copia rápida ~1-2s).

Flujo:
1. Copia el Spreadsheet maestro en Drive (instantáneo)
2. Lo hace público en solo lectura
3. Retorna el link para el cliente

Uso:
    py -m proyecto_ecxel.share_catalog --cedula NUMERO_DOCUMENTO
    py -m proyecto_ecxel.share_catalog --listar
    py -m proyecto_ecxel.share_catalog --limpiar
    py -m proyecto_ecxel.share_catalog --regenerar-maestro
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

# En Windows la consola usa cp1252 y falla al imprimir emojis (⚠, ✓, 🗑️).
# Forzamos UTF-8 en la salida para evitar UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Cargar credenciales desde .env (en la raíz del proyecto) ANTES de leer os.getenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Configuración
OAUTH_CREDENTIALS_FILE = "data/oauth_credentials.json"
OAUTH_TOKEN_FILE = "data/oauth_token.json"
COPIES_LOG_FILE = "data/copies_log.json"
CATALOG_XLSX = "data/catalogo_alianxa.xlsx"
MASTER_ID_FILE = "data/master_drive_id.json"

# Service Account (opcional). Si se define GOOGLE_SERVICE_ACCOUNT_FILE y el
# archivo existe, se usa autenticación por service account en vez del OAuth
# personal. Como una service account NO tiene almacenamiento propio en Drive,
# los archivos se crean dentro de la carpeta compartida GDRIVE_FOLDER_ID (una
# carpeta de un Drive real que se compartió con el email de la service account).
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
# Expiración corta: la copia en Sheets se autodestruye rápido para evitar
# que el link se reenvíe y lo abra un tercero (enfoque original de ~30s).
COPY_EXPIRY_MINUTES = 0.5  # 30 segundos

# ═══════════ Services Singleton ═══════════
_drive_service = None
_sheets_service = None
_services_lock = threading.Lock()


def _using_service_account() -> bool:
    """True si hay que autenticar con service account (archivo configurado y existe)."""
    return bool(SERVICE_ACCOUNT_FILE) and os.path.exists(SERVICE_ACCOUNT_FILE)


def get_oauth_credentials():
    """Obtiene credenciales de Google.

    Prioridad:
      1. Service Account (si GOOGLE_SERVICE_ACCOUNT_FILE apunta a un JSON válido).
         No caduca ni necesita navegador — ideal para producción/contenedor.
      2. OAuth de usuario (data/oauth_token.json) — modo original.
    """
    # 1. Service account (producción)
    if _using_service_account():
        return ServiceAccountCredentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )

    # 2. OAuth de usuario (modo original)
    creds = None
    if os.path.exists(OAUTH_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(OAUTH_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # En un contenedor NO hay navegador: abrir el flujo interactivo
            # colgaría el proceso. Fallamos con un mensaje accionable.
            if os.getenv("RUNNING_IN_DOCKER"):
                raise RuntimeError(
                    "OAuth de Google inválido dentro del contenedor y sin refresh_token "
                    "utilizable. Configura GOOGLE_SERVICE_ACCOUNT_FILE + GDRIVE_FOLDER_ID "
                    "(recomendado) o monta un data/oauth_token.json válido. El flujo "
                    "interactivo (run_local_server) está deshabilitado en producción."
                )
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(OAUTH_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def get_drive_service():
    """Retorna instancia cacheada del servicio de Drive (thread-safe)."""
    global _drive_service
    with _services_lock:
        if _drive_service is None:
            creds = get_oauth_credentials()
            _drive_service = build("drive", "v3", credentials=creds)
        else:
            try:
                creds = _drive_service._http.credentials
                if creds and hasattr(creds, 'expired') and creds.expired:
                    creds.refresh(Request())
                    _drive_service = build("drive", "v3", credentials=creds)
            except Exception:
                creds = get_oauth_credentials()
                _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


def get_sheets_service():
    """Retorna instancia cacheada del servicio de Sheets (thread-safe)."""
    global _sheets_service
    with _services_lock:
        if _sheets_service is None:
            creds = get_oauth_credentials()
            _sheets_service = build("sheets", "v4", credentials=creds)
        else:
            try:
                creds = _sheets_service._http.credentials
                if creds and hasattr(creds, 'expired') and creds.expired:
                    creds.refresh(Request())
                    _sheets_service = build("sheets", "v4", credentials=creds)
            except Exception:
                creds = get_oauth_credentials()
                _sheets_service = build("sheets", "v4", credentials=creds)
    return _sheets_service


# ═══════════ Log de copias ═══════════

_log_lock = threading.Lock()


def get_copies_log():
    if os.path.exists(COPIES_LOG_FILE):
        with open(COPIES_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_copies_log(copies):
    os.makedirs(os.path.dirname(COPIES_LOG_FILE), exist_ok=True)
    tmp = COPIES_LOG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(copies, f, ensure_ascii=False, indent=2)
    os.replace(tmp, COPIES_LOG_FILE)


def _append_copy_record(record: dict):
    """Append atómico de un registro al log (thread-safe)."""
    with _log_lock:
        copies = get_copies_log()
        copies.append(record)
        save_copies_log(copies)


# ═══════════ Generación del maestro ═══════════

def generate_catalog_xlsx():
    """
    Genera el catálogo maestro como Google Spreadsheet nativo.
    Elimina el maestro anterior antes de crear uno nuevo.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from proyecto_ecxel.catalogo_sheets import generate_master_spreadsheet

    drive = get_drive_service()
    sheets = get_sheets_service()

    # ¿Existe ya un maestro? Si sí y sigue en Drive, lo REUTILIZAMOS (mismo ID/URL,
    # sin duplicados). Solo creamos uno nuevo si no hay registro o el archivo se borró.
    existing_id = None
    if os.path.exists(MASTER_ID_FILE):
        try:
            with open(MASTER_ID_FILE, "r") as f:
                old_data = json.load(f)
            candidate = old_data.get("file_id")
            if candidate:
                try:
                    drive.files().get(fileId=candidate, fields="id").execute()
                    existing_id = candidate  # sigue existiendo -> reutilizar
                    print(f"  Reutilizando maestro existente: {existing_id}")
                except HttpError as he:
                    status = getattr(he.resp, "status", None)
                    if str(status) == "404":
                        print(f"  Maestro anterior ya no existe ({candidate}), se creará uno nuevo.")
                    else:
                        raise
        except Exception as e:
            print(f"  Aviso: no se pudo verificar maestro anterior: {e}")

    if existing_id:
        # Reutilizar el mismo archivo (mismo link para siempre)
        spreadsheet_id = generate_master_spreadsheet(sheets, existing_id=existing_id)
    elif GDRIVE_FOLDER_ID:
        # Crear nuevo dentro de la carpeta (primera vez o si se borró)
        spreadsheet_id = generate_master_spreadsheet(
            sheets, drive_service=drive, folder_id=GDRIVE_FOLDER_ID
        )
    else:
        spreadsheet_id = generate_master_spreadsheet(sheets)

    # Guardar ID del maestro — escritura atómica para evitar corrupción
    # si el proceso se interrumpe (causó duplicado de maestros el 14 oct)
    os.makedirs(os.path.dirname(MASTER_ID_FILE), exist_ok=True)
    tmp_master = MASTER_ID_FILE + ".tmp"
    with open(tmp_master, "w") as f:
        json.dump({"file_id": spreadsheet_id, "type": "spreadsheet"}, f)
    os.replace(tmp_master, MASTER_ID_FILE)

    print(f"  ✓ Maestro Spreadsheet creado: {spreadsheet_id}")
    return spreadsheet_id


def _move_to_shared_folder(drive, file_id):
    """Mueve un archivo a la carpeta compartida GDRIVE_FOLDER_ID (si está definida).

    Necesario cuando se usa service account: los archivos deben vivir dentro de
    una carpeta de un Drive con almacenamiento real. Con OAuth de usuario es
    opcional (si no hay carpeta configurada, no hace nada)."""
    if not GDRIVE_FOLDER_ID:
        return
    try:
        # Quitar los padres actuales y poner la carpeta compartida como único padre
        meta = drive.files().get(fileId=file_id, fields="parents").execute()
        prev_parents = ",".join(meta.get("parents", []))
        drive.files().update(
            fileId=file_id,
            addParents=GDRIVE_FOLDER_ID,
            removeParents=prev_parents,
            fields="id,parents",
        ).execute()
        print(f"  ✓ Archivo movido a la carpeta compartida {GDRIVE_FOLDER_ID}")
    except Exception as e:
        print(f"  ⚠ No se pudo mover a la carpeta compartida: {e}")


# ═══════════ Crear copia del catálogo ═══════════

def create_catalog_copy(cedula=None, nombre=None):
    """
    Copia el Spreadsheet maestro y comparte como solo lectura.
    Si ya existe una copia activa (no expirada) para la misma cédula,
    la devuelve directamente sin crear una nueva.
    """
    drive = get_drive_service()

    # ── Deduplicación: si ya hay una copia activa para esta cédula, reutilizarla ──
    if cedula:
        with _log_lock:
            copies = get_copies_log()
            now = datetime.now()
            for existing in copies:
                if existing.get("cedula") == cedula:
                    try:
                        expires = datetime.fromisoformat(existing["expires_at"])
                    except Exception:
                        continue
                    if now <= expires:
                        # Verificar que el archivo sigue vivo en Drive
                        try:
                            drive.files().get(fileId=existing["file_id"], fields="id").execute()
                            print(f"  ✓ Reutilizando copia activa para {cedula}: {existing['file_id']}")
                            return existing
                        except Exception:
                            # Archivo borrado manualmente — continuar para crear uno nuevo
                            pass

    # ── Limpieza previa: eliminar copias expiradas antes de crear ──
    try:
        cleanup_expired_copies()
    except Exception as e:
        print(f"  ⚠ Cleanup previo falló: {e}")

    # Nombre del archivo
    timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    if cedula and nombre:
        file_name = f"Catálogo - {nombre} ({timestamp})"
    else:
        file_name = f"Catálogo - Consulta ({timestamp})"

    # Obtener ID del maestro
    master_id = None
    if os.path.exists(MASTER_ID_FILE):
        with open(MASTER_ID_FILE, "r") as f:
            master_data = json.load(f)
        master_id = master_data.get("file_id")

    if not master_id:
        print("  ⚠ No hay maestro, generando...")
        master_id = generate_catalog_xlsx()

    # 1. Copiar maestro (instantáneo para Spreadsheets)
    file_id = None
    web_link = None

    # Cuerpo de la copia. Si hay carpeta compartida, la copia nace directamente
    # dentro de ella (copy sí acepta 'parents', a diferencia de sheets.create).
    copy_body = {
        "name": file_name,
        "copyRequiresWriterPermission": True,
    }
    if GDRIVE_FOLDER_ID:
        copy_body["parents"] = [GDRIVE_FOLDER_ID]

    print(f"  Copiando maestro...")
    try:
        copy = drive.files().copy(
            fileId=master_id,
            body=copy_body,
            fields="id,webViewLink"
        ).execute()
        file_id = copy["id"]
        web_link = copy.get("webViewLink", "")
    except Exception as e:
        print(f"  ❌ Error copiando maestro: {e}")
        # Si falla, intentar regenerar maestro
        master_id = generate_catalog_xlsx()
        copy = drive.files().copy(
            fileId=master_id,
            body=copy_body,
            fields="id,webViewLink"
        ).execute()
        file_id = copy["id"]
        web_link = copy.get("webViewLink", "")

    # 2. Hacer público solo lectura (acceso por link, sin login Google)
    print("  Configurando permisos...")
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()

    # 2.0 Bloquear que el visor pueda re-compartir, descargar o copiar.
    #     Sin esto, Google muestra el botón "Compartir" y el link se reenvía.
    try:
        drive.files().update(
            fileId=file_id,
            body={
                "writersCanShare": False,          # nadie más que el owner puede compartir
                "viewersCanCopyContent": False,    # lectores no pueden copiar/descargar contenido
                "copyRequiresWriterPermission": True,
            },
        ).execute()
        print("  ✓ Re-compartir / descarga / copia bloqueados para el visor")
    except Exception as e:
        print(f"  ⚠ No se pudieron endurecer permisos: {e}")

    # 2.1 En un solo batchUpdate: habilitar URLs externas + eliminar hojas INACTIVOS
    try:
        sheets = get_sheets_service()

        # Obtener IDs de hojas a eliminar (solo necesitamos este get)
        sp = sheets.spreadsheets().get(
            spreadsheetId=file_id,
            fields="sheets.properties"
        ).execute()

        requests_batch = [
            {
                "updateSpreadsheetProperties": {
                    "properties": {"importFunctionsExternalUrlAccessAllowed": True},
                    "fields": "importFunctionsExternalUrlAccessAllowed",
                }
            }
        ]

        hojas_eliminadas = []
        hojas_a_proteger = []   # IDs de hojas que quedan y deben protegerse
        for sheet in sp["sheets"]:
            title    = sheet["properties"]["title"]
            sheet_id = sheet["properties"]["sheetId"]
            if title in ("INACTIVOS", "BUSCADOR", "DATOS"):
                requests_batch.append({
                    "deleteSheet": {"sheetId": sheet_id}
                })
                hojas_eliminadas.append(title)
            else:
                hojas_a_proteger.append(sheet_id)

        # Proteger todas las hojas que quedan: nadie puede editar ninguna celda
        for sid in hojas_a_proteger:
            requests_batch.append({
                "addProtectedRange": {
                    "protectedRange": {
                        "range": {"sheetId": sid},
                        "description": "Catálogo protegido — solo lectura",
                        "warningOnly": False,   # bloqueo real, no solo advertencia
                        "editors": {"users": [], "groups": [], "domainUsersCanEdit": False},
                    }
                }
            })

        sheets.spreadsheets().batchUpdate(
            spreadsheetId=file_id,
            body={"requests": requests_batch}
        ).execute()

        if hojas_eliminadas:
            print(f"  ✓ Hojas eliminadas: {', '.join(hojas_eliminadas)}")
        if hojas_a_proteger:
            print(f"  ✓ {len(hojas_a_proteger)} hoja(s) protegidas contra edición")
        print("  ✓ Acceso externo habilitado / solo lectura configurado")

    except Exception as e:
        print(f"  ⚠ Error configurando copia: {e}")

    link = web_link

    # 3. Registrar
    copy_record = {
        "file_id": file_id,
        "name": file_name,
        "link": link,
        "cedula": cedula or "N/A",
        "nombre_cliente": nombre or "N/A",
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(minutes=COPY_EXPIRY_MINUTES)).isoformat(),
    }
    _append_copy_record(copy_record)

    print(f"\n{'═'*60}")
    print(f" ✓ CATÁLOGO COMPARTIDO EXITOSAMENTE")
    print(f"{'═'*60}")
    print(f"  Nombre:   {file_name}")
    print(f"  Link:     {link}")
    print(f"  Expira:   {COPY_EXPIRY_MINUTES} minutos")
    if cedula:
        print(f"  Cliente:  {nombre} (CC {cedula})")
    print(f"{'═'*60}\n")

    return copy_record


# ═══════════ Limpieza ═══════════

def cleanup_expired_copies():
    """Elimina copias expiradas de Google Drive.

    También deduplica el log: si hay varias copias activas para la misma cédula
    (puede ocurrir por recargas rápidas o reinicios), elimina las más antiguas y
    conserva solo la más reciente.
    """
    drive = get_drive_service()

    with _log_lock:
        copies = get_copies_log()
        now = datetime.now()
        active = []
        deleted = 0

        # Paso 1 — eliminar expiradas
        for c in copies:
            try:
                expires = datetime.fromisoformat(c["expires_at"])
            except Exception:
                expires = now  # registro corrupto → tratar como expirado

            if now > expires:
                try:
                    drive.files().delete(fileId=c["file_id"]).execute()
                    deleted += 1
                    print(f"  ✗ Eliminada (expirada): {c['name']}")
                except Exception as e:
                    # Si el archivo ya no existe en Drive (404), igual lo sacamos del log
                    err_str = str(e)
                    if "404" in err_str or "notFound" in err_str or "File not found" in err_str:
                        deleted += 1
                        print(f"  ✗ Ya no existe en Drive (removido del log): {c.get('name', '?')}")
                    else:
                        # Error real: conservar en log para reintentar en el próximo ciclo
                        print(f"  ⚠ No se pudo eliminar {c.get('name', '?')}: {e}")
                        active.append(c)
            else:
                active.append(c)

        # Paso 2 — deduplicar por cédula: quedarse solo con la copia más reciente por cliente
        seen_cedulas: dict = {}
        deduped = []
        # Ordenar por created_at descendente para que el primero visto sea el más reciente
        active_sorted = sorted(
            active,
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )
        for c in active_sorted:
            cedula_key = c.get("cedula", "N/A")
            if cedula_key == "N/A" or cedula_key not in seen_cedulas:
                seen_cedulas[cedula_key] = True
                deduped.append(c)
            else:
                # Copia duplicada activa — eliminarla de Drive también
                try:
                    drive.files().delete(fileId=c["file_id"]).execute()
                    deleted += 1
                    print(f"  ✗ Eliminada (duplicada): {c['name']}")
                except Exception as e:
                    err_str = str(e)
                    if "404" in err_str or "notFound" in err_str or "File not found" in err_str:
                        deleted += 1
                        print(f"  ✗ Duplicada ya no existe en Drive: {c.get('name', '?')}")
                    else:
                        print(f"  ⚠ No se pudo eliminar duplicada {c.get('name', '?')}: {e}")
                        deduped.append(c)  # reintentar luego

        save_copies_log(deduped)

    print(f"\nEliminadas: {deleted} | Activas: {len(deduped)}")
    return {"deleted": deleted, "active": len(deduped)}


def list_copies():
    """Lista copias generadas."""
    copies = get_copies_log()
    if not copies:
        print("No hay copias generadas.")
        return

    print(f"\n{'═'*80}")
    print(f" COPIAS GENERADAS ({len(copies)})")
    print(f"{'═'*80}")
    print(f"{'#':<4} {'Cliente':<25} {'Cédula':<12} {'Creada':<18} {'Estado':<10}")
    print(f"{'─'*80}")

    now = datetime.now()
    for i, c in enumerate(copies, 1):
        expires = datetime.fromisoformat(c["expires_at"])
        estado = "✔ Activa" if now < expires else "✗ Expirada"
        created = c["created_at"][:16].replace("T", " ")
        nombre = c.get("nombre_cliente", "N/A")[:23]
        cedula = c.get("cedula", "N/A")

        print(f"{i:<4} {nombre:<25} {cedula:<12} {created:<18} {estado:<10}")

    print(f"{'═'*80}\n")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:] if len(sys.argv) > 1 else []

    if "--listar" in args:
        list_copies()
    elif "--limpiar" in args:
        print("Limpiando copias expiradas...")
        cleanup_expired_copies()
    elif "--regenerar-maestro" in args:
        print("Regenerando maestro como Google Spreadsheet...")
        generate_catalog_xlsx()
    elif "--cedula" in args:
        idx = args.index("--cedula")
        if idx + 1 < len(args):
            cedula = args[idx + 1]
            import requests as http_requests
            from requests.auth import HTTPBasicAuth
            alegra_email = os.getenv("ALEGRA_EMAIL", "")
            alegra_token = os.getenv("ALEGRA_API_TOKEN", "")
            if not alegra_email or not alegra_token:
                print("ERROR: Faltan ALEGRA_EMAIL / ALEGRA_API_TOKEN en el archivo .env")
                sys.exit(1)
            auth = HTTPBasicAuth(alegra_email, alegra_token)
            r = http_requests.get(
                "https://api.alegra.com/api/v1/contacts",
                params={"identification": cedula, "limit": 5},
                auth=auth
            )
            nombre = "Cliente"
            if r.status_code == 200:
                for contact in r.json():
                    if str(contact.get("identification", "")).strip() == cedula.strip():
                        nombre = contact.get("name", "Cliente")
                        break
            create_catalog_copy(cedula=cedula, nombre=nombre)
        else:
            print("Uso: --cedula NUMERO_DOCUMENTO")
    else:
        create_catalog_copy()
