"""
Web App - Catálogo con token de un solo uso + verificación OTP por email.

Flujo:
1. Login con cédula → valida en Alegra (priceList INSTALADOR)
2. Muestra el email enmascarado del contacto → usuario da clic en "Enviar código"
3. Se envía OTP de 6 dígitos por email (Gmail SMTP)
4. Usuario ingresa código → se verifica contra la sesión OTP
5. Si es correcto → genera catálogo (web o Google Sheets) + token único
6. Botón "Ver Catálogo" → /ver/TOKEN o /excel/TOKEN → token consumido
"""

import asyncio
import csv
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
from io import StringIO
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

import mysql.connector

from fastapi import Depends, FastAPI, HTTPException, Request, Form, Cookie, Query, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
import re
import requests as http_requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# ═══════════ Setup de paths (una sola vez al inicio) ═══════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from proyecto_ecxel.share_catalog import (
    create_catalog_copy,
    get_drive_service,
    get_sheets_service,
    generate_catalog_xlsx,
    cleanup_expired_copies,
    MASTER_ID_FILE,
    CATALOG_XLSX,
)

# ═══════════ Configuración ═══════════
ALEGRA_EMAIL = os.getenv("ALEGRA_EMAIL", "")
ALEGRA_TOKEN = os.getenv("ALEGRA_API_TOKEN", "")
if not ALEGRA_EMAIL or not ALEGRA_TOKEN:
    raise RuntimeError(
        "Faltan credenciales de Alegra. Define ALEGRA_EMAIL y ALEGRA_API_TOKEN en el archivo .env"
    )
ALEGRA_AUTH = HTTPBasicAuth(ALEGRA_EMAIL, ALEGRA_TOKEN)

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
# Nombre visible del remitente (lo que ve el cliente como "De:")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Catálogo Alianxa")
_cookie_secure_setting = os.getenv("COOKIE_SECURE", "").strip().lower()
COOKIE_SECURE = (
    _cookie_secure_setting in ("1", "true", "yes", "on")
    if _cookie_secure_setting
    else os.getenv("RUNNING_IN_DOCKER") == "1"
)

TOKENS_FILE = PROJECT_ROOT / "data" / "active_tokens.json"
TOKEN_MAX_AGE_SECONDS = 600  # 10 minutos
OTP_SESSION_MAX_AGE_SECONDS = 600

# Lock para acceso thread-safe al archivo de tokens
_tokens_lock = threading.Lock()

# Sesiones OTP en memoria: {session_id: {cedula, nombre, phone, verification_id, ...}}
_otp_sessions = {}
_otp_lock = threading.Lock()
_BOGOTA = timezone(timedelta(hours=-5))


# ── Rate limiting en memoria ──────────────────────────────────────────────────
# uvicorn corre con 1 worker, un dict compartido es suficiente.
# _rate_hits: { clave: deque([timestamp, ...]) }
import hmac
from collections import defaultdict, deque

_rate_hits: dict = defaultdict(deque)
_rate_lock = threading.Lock()

def _rate_allow(key: str, max_n: int, window_s: int) -> bool:
    """Devuelve True si la acción está permitida, False si se superó el límite.
    
    key     — identificador único (ej. "auth:ip:1.2.3.4" o "verify:session_id")
    max_n   — máximo de acciones permitidas en la ventana
    window_s — tamaño de la ventana en segundos
    """
    now = time.time()
    with _rate_lock:
        dq = _rate_hits[key]
        # Eliminar timestamps fuera de la ventana
        while dq and now - dq[0] > window_s:
            dq.popleft()
        if len(dq) >= max_n:
            return False
        dq.append(now)
        return True

def _get_client_ip(request: "Request") -> str:  # noqa: F821 — Request se importa más abajo
    """Extrae IP real considerando proxy/ngrok."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _now_colombia() -> datetime:
    return datetime.now(_BOGOTA).replace(tzinfo=None)


def _cleanup_expired_otp_sessions(now: float | None = None) -> int:
    current_time = time.time() if now is None else now
    with _otp_lock:
        expired_ids = [
            session_id
            for session_id, session in _otp_sessions.items()
            if current_time - session.get("created", 0) > OTP_SESSION_MAX_AGE_SECONDS
        ]
        for session_id in expired_ids:
            _otp_sessions.pop(session_id, None)
    return len(expired_ids)


# ═══════════ Base de datos MySQL — log OTP ═══════════

_DB_CONFIG = {
    "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER",     "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "inventario_local"),
    "charset":  "utf8mb4",
}

# ═══════════ Caché de catálogo (JSON persistente) ═══════════
# El caché se regenera en sync_db.py después de cada sincronización.
# app.py solo lee — nunca hace queries MySQL para servir el catálogo.
from src.catalog_cache import read_instalador as _read_cache_instalador
from src.catalog_cache import read_cliente    as _read_cache_cliente
from src.catalog_cache import read_html_cliente as _read_html_cliente


def _log_otp_success(id_cliente: str, correo: str, modo: str) -> None:
    """Inserta un registro en otp_log cuando el cliente valida el OTP exitosamente."""
    try:
        conn = mysql.connector.connect(**_DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO otp_log (id_cliente, correo, fecha_hora, modo)
            VALUES (%s, %s, %s, %s)
            """,
            (id_cliente, correo, _now_colombia(), modo),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        # No interrumpimos el flujo del usuario si falla el log
        print(f"[otp_log] Error al registrar: {exc}", flush=True)


def _log_acceso_catalogo(
    nombre: str,
    documento: str | None,
    email: str | None,
    telefono: str | None,
    origen: str,
    tipo_cliente: str,
    alegra_registrado: bool,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Registra cada acceso válido a cualquiera de los catálogos.

    origen: web, excel, publico
    tipo_cliente: instalador_alegra, instalador_no_alegra, cliente_publico, cliente_publico_no_alegra
    alegra_registrado: True/False para diferenciar si la persona está o no en Alegra.
    """
    try:
        conn = mysql.connector.connect(**_DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO registro_accesos
                (nombre, documento, email, telefono, origen, tipo_cliente, alegra_registrado, ip, user_agent, fecha)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                (nombre or "")[:200],
                (documento or "")[:50],
                (email or "")[:150],
                (telefono or "")[:50],
                (origen or "web")[:20],
                (tipo_cliente or "desconocido")[:40],
                1 if alegra_registrado else 0,
                (ip or "")[:45],
                (user_agent or "")[:500],
                _now_colombia(),
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        print(f"[registro_accesos] Error al registrar acceso: {exc}", flush=True)


def _registrar_acceso_instalador(
    nombre: str,
    documento: str,
    email: str,
    telefono: str,
    modo: str,
    ip: str,
    user_agent: str,
) -> bool:
    """Guarda OTP y acceso en una sola transacción obligatoria."""
    conn = None
    try:
        conn = mysql.connector.connect(**_DB_CONFIG)
        cur = conn.cursor()
        ahora = _now_colombia()
        cur.execute(
            """
            INSERT INTO otp_log (id_cliente, correo, fecha_hora, modo)
            VALUES (%s, %s, %s, %s)
            """,
            (documento[:50], email[:150], ahora, modo[:10]),
        )
        cur.execute(
            """
            INSERT INTO registro_accesos
                (nombre, documento, email, telefono, origen, tipo_cliente,
                 alegra_registrado, ip, user_agent, fecha)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                nombre[:200],
                documento[:50],
                email[:150],
                telefono[:50],
                modo[:20],
                "instalador_alegra",
                1,
                ip[:45],
                user_agent[:500],
                ahora,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as exc:
        if conn is not None:
            conn.rollback()
            conn.close()
        print(f"[acceso_instalador] No se pudo registrar: {exc}", flush=True)
        return False


def _log_visita_cliente(nombre: str, telefono: str, ip: str, user_agent: str) -> int | None:
    """Registra o recupera el cliente en visitas_clientes (una sola fila por teléfono).

    Usa INSERT … ON DUPLICATE KEY UPDATE para que si ya existe el teléfono
    solo actualice el timestamp y devuelva el mismo id.

    Retorna el id del cliente, o None si falla.
    """
    try:
        conn = mysql.connector.connect(**_DB_CONFIG)
        cur  = conn.cursor()
        ahora = _now_colombia()

        cur.execute(
            """
            INSERT INTO visitas_clientes (nombre, telefono, ip, user_agent, fecha)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                nombre     = VALUES(nombre),
                ip         = VALUES(ip),
                user_agent = VALUES(user_agent),
                fecha      = VALUES(fecha)
            """,
            (nombre[:200], telefono[:50], (ip or "")[:45], (user_agent or "")[:500], ahora),
        )
        conn.commit()
        # LAST_INSERT_ID() = 0 cuando es UPDATE (ON DUPLICATE KEY), así que
        # hacemos un SELECT para obtener el id real.
        cur.execute("SELECT id FROM visitas_clientes WHERE telefono = %s", (telefono[:50],))
        row = cur.fetchone()
        id_cliente = row[0] if row else None
        cur.close()
        conn.close()
        return id_cliente
    except Exception as exc:
        print(f"[visitas_clientes] Error al registrar: {exc}", flush=True)
        return None


def _buscar_cliente_por_telefono(telefono: str) -> dict | None:
    """Devuelve {id, nombre, telefono} si el teléfono ya está registrado, o None."""
    try:
        conn = mysql.connector.connect(**_DB_CONFIG)
        cur  = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, nombre, telefono FROM visitas_clientes WHERE telefono = %s",
            (telefono[:50],),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row
    except Exception as exc:
        print(f"[visitas_clientes] Error al buscar: {exc}", flush=True)
        return None

# ═══════════ Persistencia de tokens (thread-safe) ═══════════

def _load_tokens() -> dict:
    if not TOKENS_FILE.exists():
        return {}
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            tokens = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}
    now = time.time()
    return {k: v for k, v in tokens.items() if now - v.get("created", 0) < 3600}


def _save_tokens(tokens: dict):
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_FILE.parent / (TOKENS_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)
    tmp.replace(TOKENS_FILE)  # atómico — evita corrupción si el proceso muere mid-write
    try:
        print(f"[_save_tokens] wrote {len(tokens)} tokens", flush=True)
    except Exception:
        pass


def _get_token(token: str) -> dict | None:
    with _tokens_lock:
        tokens = _load_tokens()
        data = tokens.pop(token, None)
        if data is not None:
            _save_tokens(tokens)
            try:
                print(f"[_get_token] consumed token {token[:8]}.., remaining={len(tokens)}", flush=True)
            except Exception:
                pass
        else:
            try:
                print(f"[_get_token] token {token[:8]}.. not found", flush=True)
            except Exception:
                pass
    return data


def _set_token(token: str, data: dict):
    with _tokens_lock:
        tokens = _load_tokens()
        tokens[token] = data
        _save_tokens(tokens)
        try:
            print(f"[_set_token] set token {token[:8]}.., total={len(tokens)}", flush=True)
        except Exception:
            pass


def _peek_token(token: str) -> dict | None:
    """Lectura no destructiva del token (no lo elimina)."""
    with _tokens_lock:
        tokens = _load_tokens()
        data = tokens.get(token)
    return data


def _consume_token(token: str) -> dict | None:
    """Elimina el token y devuelve sus datos. Úsalo cuando la acción sea final."""
    with _tokens_lock:
        tokens = _load_tokens()
        data = tokens.pop(token, None)
        if data is not None:
            _save_tokens(tokens)
            try:
                print(f"[_consume_token] removed token {token[:8]}.., remaining={len(tokens)}", flush=True)
            except Exception:
                pass
    return data


# ═══════════ Validación Alegra ═══════════

def normalize_document_type(tipo_doc: str | None) -> str:
    """Normaliza aliases de tipos de documento para la validación de Alegra.

    La API real puede devolver `PPT` para este tipo de documento, aunque algunos
    formularios lo presenten como "Permiso". Mantenemos ambos como alias válidos.
    """
    if tipo_doc is None:
        return "CC"
    tipo = str(tipo_doc).strip().upper()
    aliases = {
        "PPT": "PPT",
        "PERMISO": "PPT",
        "PERMISO_DE_PROTECCION_TEMPORAL": "PPT",
        "PEP": "PEP",
        "CC": "CC",
        "NIT": "NIT",
        "DIE": "DIE",
    }
    return aliases.get(tipo, "CC")


def _document_type_candidates(tipo_doc: str | None) -> list[str]:
    """Devuelve los tipos equivalentes que deben probarse en Alegra."""
    tipo = normalize_document_type(tipo_doc)
    if tipo in {"PPT", "PEP"}:
        return ["PPT", "PEP"]
    return [tipo]


def _buscar_contacto_alegra(identificacion: str, tipo_doc: str) -> dict | None:
    """Consulta la API de Alegra buscando un contacto por identificación y tipo de documento.
    Retorna el primer contacto que coincida exactamente, o None si no existe.
    tipo_doc: "CC", "NIT", "DIE" u otro valor válido en Alegra.
    """
    try:
        r = http_requests.get(
            "https://api.alegra.com/api/v1/contacts",
            params={"identification": identificacion, "limit": 10},
            auth=ALEGRA_AUTH,
            timeout=10,
        )
        if r.status_code != 200:
            return None
        candidates = {c.upper() for c in _document_type_candidates(tipo_doc)}
        for contact in r.json():
            # Coincidencia exacta de número Y tipo de documento
            id_match = str(contact.get("identification", "")).strip() == str(identificacion).strip()
            tipo_real = str(contact.get("identificationObject", {}).get("type", "")).upper()
            tipo_match = tipo_real in candidates
            if id_match and tipo_match:
                return contact
        return None
    except Exception:
        return None


def _validate_cedula_alegra(cedula_raw: str, tipo_doc_usuario: str = "CC") -> dict:
    """Valida el documento ingresado siguiendo la lógica de prioridad por tipo y longitud.

    Si el usuario seleccionó un tipo explícito (CE), se busca directamente con ese tipo.
    Para CC, se usa la lógica de prioridad por longitud:
      10 dígitos → CC primero; si no existe, NIT con los 9 primeros dígitos
       9 dígitos → NIT directo
       otro      → CC; si no existe, CE

    Retorna:
      dict con datos del contacto  → si encontró un contacto con lista INSTALADOR
      {"error": "not_found"}       → documento no existe en ningún tipo
      {"error": "wrong_list"}      → existe pero no tiene lista INSTALADOR
      {"error": "api_error"}       → fallo de red o HTTP inesperado
    """
    import re
    # Solo dígitos para todos los tipos
    cedula = re.sub(r"\D", "", str(cedula_raw))

    try:
        # ── NIT: probar primero el valor exacto y luego la variante sin DV ──
        if tipo_doc_usuario.upper() == "NIT":
            if len(cedula) == 10:
                intentos = [("NIT", cedula), ("NIT", cedula[:9])]
            elif len(cedula) == 9:
                intentos = [("NIT", cedula)]
            else:
                # Longitud inválida para NIT
                return {"error": "not_found"}

        # ── DIE: buscar directo con el número exacto ──
        elif tipo_doc_usuario.upper() == "DIE":
            intentos = [("DIE", cedula)]

        # ── PEP / PPT (Permiso especial de permanencia / Permiso):
        #    la API real puede devolver `PPT` en algunos contactos, así que
        #    intentamos ambos tipos para evitar falsos negativos. ──
        elif tipo_doc_usuario.upper() in {"PEP", "PPT"}:
            intentos = [("PEP", cedula), ("PPT", cedula)]

        # ── CC: 10 dígitos reintenta como NIT (cubre cuentas duplicadas CC/NIT)
        #       cualquier otra longitud busca solo CC ──
        elif len(cedula) == 10:
            intentos = [("CC", cedula), ("NIT", cedula[:9])]
        else:
            intentos = [("CC", cedula)]

        found_contact = None
        for tipo_doc, num in intentos:
            contact = _buscar_contacto_alegra(num, tipo_doc)
            if contact is not None:
                found_contact = contact
                break

        if found_contact is None:
            return {"error": "not_found"}

        price_list = found_contact.get("priceList", {})
        if not (isinstance(price_list, dict) and price_list.get("name") == "INSTALADOR"):
            # Encontrado pero no tiene lista INSTALADOR
            tipo_encontrado = found_contact.get("identificationObject", {}).get("type", "")
            print(f"  ⚠ Contacto encontrado como {tipo_encontrado} pero lista={price_list.get('name','?')} — no es INSTALADOR")
            return {"error": "wrong_list"}

        email = found_contact.get("email", "") or ""
        phone = found_contact.get("phonePrimary", "") or found_contact.get("mobile", "") or ""
        return {
            "nombre": found_contact.get("name", "Cliente").strip(),
            "cedula": cedula,
            "email": email,
            "phone": phone,
        }
    except Exception as e:
        print(f"  ✗ Error validando cédula {cedula_raw}: {e}")
        return {"error": "api_error"}


# ═══════════ OTP por Email (Gmail SMTP) ═══════════

def _mask_email(email: str) -> str:
    """Enmascara email: na****@gmail.com"""
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) > 2:
        masked = local[:2] + "****"
    else:
        masked = local[0] + "****"
    return f"{masked}@{domain}"


def _generate_otp_code() -> str:
    """Genera código OTP de 6 dígitos (criptográficamente seguro)."""
    return str(secrets.randbelow(900000) + 100000)


def _send_otp_email(to_email: str, code: str, nombre: str) -> bool:
    """Envía código OTP por email vía Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Código de verificación: {code}"
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_EMAIL}>"
        msg["To"] = to_email

        html_body = f"""
        <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 30px;">
            <div style="background: linear-gradient(135deg, #1a2a4a, #2d4a7a); padding: 25px; border-radius: 12px 12px 0 0; text-align: center;">
                <h1 style="color: white; margin: 0; font-size: 1.3rem;">Código de Verificación</h1>
            </div>
            <div style="background: white; padding: 30px; border: 1px solid #e0e0e0; border-radius: 0 0 12px 12px;">
                <p style="color: #333; font-size: 0.95rem;">Hola <strong>{escape(nombre)}</strong>,</p>
                <p style="color: #666; font-size: 0.9rem;">Tu código de verificación para acceder al catálogo es:</p>
                <div style="background: #f0f4ff; border: 2px solid #2d4a7a; border-radius: 10px; padding: 20px; text-align: center; margin: 20px 0;">
                    <span style="font-size: 2.2rem; font-weight: 800; color: #1a2a4a; letter-spacing: 8px;">{code}</span>
                </div>
                <p style="color: #888; font-size: 0.8rem;">Este código expira en 5 minutos. No lo compartas con nadie.</p>
                <p style="color: #888; font-size: 0.8rem; margin-top: 12px;">Si no recibes el correo, revisa tu bandeja de spam o correo no deseado.</p>
            </div>
        </div>
        """

        text_body = f"Hola {nombre}, tu código de verificación es: {code}. Expira en 5 minutos."

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())

        print(f"  ✓ OTP enviado a {to_email}")
        return True
    except Exception as e:
        print(f"  ⚠ Error enviando email OTP: {e}")
        return False


# ═══════════ Limpieza periódica ═══════════

async def _cleanup_loop():
    """Limpia copias expiradas de Drive frecuentemente (cada 30s) para que las
    copias de vida corta se borren pronto y el link no quede reutilizable."""
    while True:
        await asyncio.sleep(30)
        try:
            removed_sessions = _cleanup_expired_otp_sessions()
            if removed_sessions:
                print(f"  🧹 Sesiones OTP expiradas eliminadas: {removed_sessions}", flush=True)
            await asyncio.to_thread(cleanup_expired_copies)
        except Exception as e:
            print(f"  ⚠ Error en limpieza periódica: {e}")


# ═══════════ Verificación del maestro en Drive ═══════════

def _ensure_master_exists():
    master_id_path = PROJECT_ROOT / MASTER_ID_FILE
    if master_id_path.exists():
        try:
            with open(master_id_path, "r") as f:
                master_data = json.load(f)
            master_id = master_data.get("file_id")
            if master_id:
                drive = get_drive_service()
                try:
                    drive.files().get(fileId=master_id, fields="id").execute()
                    print(f"  ✓ Maestro verificado en Drive: {master_id}")
                    return
                except Exception:
                    print(f"  ⚠ Maestro no encontrado en Drive, regenerando...")
        except (json.JSONDecodeError, IOError):
            pass

    # generate_catalog_xlsx() crea el maestro como Spreadsheet nativo dentro de la
    # carpeta compartida y guarda su ID en MASTER_ID_FILE. No hay que subir ningún
    # .xlsx aparte (eso además fallaría con service account por falta de quota).
    print("  📄 Generando maestro (Google Spreadsheet)...")
    new_master_id = generate_catalog_xlsx()
    print(f"  ✓ Maestro listo: {new_master_id}")


# ═══════════ Lifespan ═══════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Iniciando app — verificando maestro en Drive...")
    try:
        await asyncio.to_thread(_ensure_master_exists)
    except Exception as e:
        print(f"  ⚠ Error verificando maestro: {e}")

    print("🧹 Limpiando copias expiradas...")
    try:
        await asyncio.to_thread(cleanup_expired_copies)
    except Exception as e:
        print(f"  ⚠ Error limpiando copias: {e}")

    # Pre-calentar servicios Drive y Sheets para que el primer cliente
    # no pague el costo de inicialización OAuth (~1s extra)
    print("🔥 Pre-calentando servicios Drive/Sheets...")
    try:
        await asyncio.to_thread(get_drive_service)
        await asyncio.to_thread(get_sheets_service)
        print("  ✓ Servicios Drive/Sheets listos")
    except Exception as e:
        print(f"  ⚠ Error pre-calentando servicios: {e}")

    # Regenerar caché en background para no bloquear el arranque de la app.
    # La app sirve el caché anterior mientras se genera el nuevo.
    async def _rebuild_cache():
        try:
            from src.catalog_cache import rebuild_all
            print("📦 Regenerando caché de catálogo (background)...")
            await asyncio.to_thread(rebuild_all)
            print("  ✓ Caché regenerado")
        except Exception as e:
            print(f"  ⚠ Error regenerando caché: {e}")

    rebuild_task = asyncio.create_task(_rebuild_cache())

    # Loop periódico de limpieza cada 10 minutos
    cleanup_task = asyncio.create_task(_cleanup_loop())

    yield

    cleanup_task.cancel()
    rebuild_task.cancel()
    print(" App finalizada")


# ═══════════ App FastAPI ═══════════

app = FastAPI(title="Catálogo Precios", docs_url=None, redoc_url=None, lifespan=lifespan)
# Comprimir respuestas grandes (HTML pre-renderizado) para mejorar tiempos de descarga
app.add_middleware(GZipMiddleware, minimum_size=512)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))
templates.env.globals["whatsapp_phone"] = re.sub(
    r"\D", "", os.getenv("WHATSAPP_PHONE", "")
)
_reports_basic_auth = HTTPBasic(auto_error=False)


def _require_reports_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_reports_basic_auth),
) -> str:
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if request.url.scheme != "https" and not is_loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El panel solo permite HTTP desde localhost; los accesos remotos requieren HTTPS.",
        )
    username = os.getenv("REPORTS_ADMIN_USER", "")
    password = os.getenv("REPORTS_ADMIN_PASSWORD", "")
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El panel de reportes no está configurado.",
        )
    if (
        credentials is None
        or not secrets.compare_digest(credentials.username, username)
        or not secrets.compare_digest(credentials.password, password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _access_report_dates(
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date]:
    today = datetime.now(_BOGOTA).date()
    end = end_date or today
    start = start_date or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha inicial no puede ser posterior a la fecha final.",
        )
    if (end - start).days > 366:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El período máximo permitido es de 367 días.",
        )
    return start, end


# ═══════════ Anti-caché ═══════════
# El navegador cacheaba las páginas del flujo (login/OTP) y, al usar el botón
# "atrás", re-mostraba versiones viejas — perdiendo el modo (excel/web) y
# terminando en el catálogo equivocado. Con no-store el navegador siempre pide
# la página fresca al servidor, que aplica el modo correcto.
@app.middleware("http")
async def _no_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # No cachear el flujo de acceso ni las vistas de catálogo (contenido sensible/efímero)
    if path == "/" or path.startswith(("/web", "/excel", "/auth", "/send-otp", "/verify-otp", "/ver", "/publico", "/admin")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ═══════════ Rutas ═══════════

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    """Raíz: redirige siempre al catálogo Excel (entrada por defecto)."""
    return RedirectResponse(url="/excel", status_code=302)


@app.get("/web", response_class=HTMLResponse)
async def login_catalogo(request: Request):
    """Entry point para el catálogo web."""
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None, "modo": "web", "tipo_doc": "CC"})


@app.get("/excel", response_class=HTMLResponse)
async def login_excel(request: Request):
    """Entry point para Google Sheets."""
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None, "modo": "excel", "tipo_doc": "CC"})


@app.get("/admin/accesos", response_class=HTMLResponse)
async def access_reports_page(
    request: Request,
    start_date: date | None = Query(None, alias="desde"),
    end_date: date | None = Query(None, alias="hasta"),
    origin: str | None = Query(None, alias="origen"),
    _: str = Depends(_require_reports_admin),
):
    """Panel interno con métricas agregadas de accesos exitosos."""
    start, end = _access_report_dates(start_date, end_date)
    if origin not in (None, "", "web", "excel", "publico"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Origen de acceso no válido.",
        )
    from src.access_reports import fetch_access_report

    try:
        report = await asyncio.to_thread(
            fetch_access_report, start, end, origin or None
        )
    except mysql.connector.Error as exc:
        print(f"[access_reports] Error consultando MySQL: {exc}", flush=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No fue posible consultar los registros de acceso.",
        ) from exc

    return templates.TemplateResponse(
        request=request,
        name="access_reports.html",
        context={
            "summary": report["summary"],
            "by_origin": report["by_origin"],
            "frequent_clients": report["frequent_clients"],
            "new_clients": report["new_clients"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "origin": origin or "",
        },
    )


@app.get("/admin/accesos/export.csv")
async def export_access_report(
    start_date: date | None = Query(None, alias="desde"),
    end_date: date | None = Query(None, alias="hasta"),
    origin: str | None = Query(None, alias="origen"),
    _: str = Depends(_require_reports_admin),
):
    """Exporta solo indicadores agregados; no incluye datos personales."""
    start, end = _access_report_dates(start_date, end_date)
    if origin not in (None, "", "web", "excel", "publico"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Origen de acceso no válido.",
        )
    from src.access_reports import fetch_access_report

    try:
        report = await asyncio.to_thread(
            fetch_access_report, start, end, origin or None
        )
    except mysql.connector.Error as exc:
        print(f"[access_reports] Error exportando reporte: {exc}", flush=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No fue posible consultar los registros de acceso.",
        ) from exc

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Indicador", "Valor"])
    writer.writerow(["Desde", start.isoformat()])
    writer.writerow(["Hasta", end.isoformat()])
    writer.writerow(["Accesos exitosos", report["summary"]["total_accesses"]])
    writer.writerow(["Personas únicas", report["summary"]["unique_clients"]])
    writer.writerow(["Personas no registradas en Alegra", report["summary"]["new_clients"]])
    writer.writerow(["Clientes recurrentes", report["summary"]["frequent_clients"]])
    writer.writerow([])
    writer.writerow(["Origen", "Accesos"])
    for row in report["by_origin"]:
        writer.writerow([row["origen"], row["accesses"]])

    filename = f"reporte_accesos_{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/accesos/export.xlsx")
async def export_access_report_xlsx(
    start_date: date | None = Query(None, alias="desde"),
    end_date: date | None = Query(None, alias="hasta"),
    origin: str | None = Query(None, alias="origen"),
    _: str = Depends(_require_reports_admin),
):
    """Descarga el reporte visual en Excel, sin datos personales."""
    start, end = _access_report_dates(start_date, end_date)
    if origin not in (None, "", "web", "excel", "publico"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Origen de acceso no válido.",
        )
    from src.access_reports import build_access_report_xlsx, fetch_access_report

    try:
        report = await asyncio.to_thread(
            fetch_access_report, start, end, origin or None
        )
        content = build_access_report_xlsx(report, start, end, origin or None)
    except mysql.connector.Error as exc:
        print(f"[access_reports] Error generando Excel: {exc}", flush=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No fue posible consultar los registros de acceso.",
        ) from exc

    filename = f"reporte_accesos_{start.isoformat()}_{end.isoformat()}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/auth", response_class=HTMLResponse)
async def login(request: Request, cedula: str = Form(...), modo: str = Form("web"), tipo_doc: str = Form("CC")):
    """Valida cédula → muestra pantalla de envío de código OTP."""
    cedula = cedula.strip()
    modo = modo if modo in ("web", "excel") else "web"
    tipo_doc = normalize_document_type(tipo_doc)

    cedula_vacia = not cedula or not cedula.strip()
    cedula_invalida = not cedula.isdigit()
    if cedula_vacia or cedula_invalida:
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Ingresa un número de documento válido.",
            "modo": modo,
            "tipo_doc": tipo_doc,
        })

    # ── Rate limiting: máx 5 intentos por cédula y 10 por IP en 10 min ──────
    ip = _get_client_ip(request)
    if not _rate_allow(f"auth:cedula:{cedula}", max_n=5, window_s=600):
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Demasiados intentos para este documento. Espera unos minutos e intenta de nuevo.",
            "modo": modo,
            "tipo_doc": tipo_doc,
        })
    if not _rate_allow(f"auth:ip:{ip}", max_n=10, window_s=600):
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Demasiadas solicitudes desde tu dispositivo. Espera unos minutos.",
            "modo": modo,
            "tipo_doc": tipo_doc,
        })

    # Validar contra la base de clientes y/o documentación del sistema.
    contact = await asyncio.to_thread(_validate_cedula_alegra, cedula, tipo_doc)

    if not contact or contact.get("error"):
        error_code = contact.get("error") if contact else "api_error"
        if error_code in ("not_found", "wrong_list"):
            error_msg = (
                "No encontramos un registro válido para este documento. "
                "Verifica los datos o comunícate con tu asesor."
            )
        else:
            error_msg = (
                "No pudimos verificar tu documento en este momento. "
                "Intenta de nuevo o comunícate con tu asesor."
            )
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": error_msg,
            "modo": modo,
            "tipo_doc": tipo_doc,
        })

    # Verificar que tiene email
    email = contact.get("email", "")
    if not email or "@" not in email:
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "No tienes correo registrado para este documento. Verifica el contacto o comunícate con tu asesor.",
            "modo": modo,
            "tipo_doc": tipo_doc,
        })

    # Crear sesión OTP
    session_id = secrets.token_urlsafe(24)
    with _otp_lock:
        _otp_sessions[session_id] = {
            "cedula": contact["cedula"],
            "nombre": contact["nombre"],
            "email": email,
            "telefono": contact.get("phone", ""),
            "otp_code": None,
            "created": time.time(),
            "modo": modo,
        }

    # Mostrar pantalla de envío de código
    return templates.TemplateResponse(request=request, name="verify_send.html", context={
        "nombre": contact["nombre"],
        "masked_email": _mask_email(email),
        "session_id": session_id,
        "modo": modo,
        "error": None,
    })


@app.post("/send-otp", response_class=HTMLResponse)
async def send_otp(request: Request, session_id: str = Form(...)):
    """Envía el OTP por email al usuario."""
    with _otp_lock:
        session = _otp_sessions.get(session_id)

    if not session or time.time() - session.get("created", 0) > OTP_SESSION_MAX_AGE_SECONDS:
        return RedirectResponse(url="/", status_code=302)

    modo = session.get("modo", "web")

    # ── Rate limiting: máx 1 reenvío por minuto y 3 por sesión ───────────────
    if not _rate_allow(f"send-otp:min:{session_id}", max_n=1, window_s=60):
        return templates.TemplateResponse(request=request, name="verify_send.html", context={
            "nombre": session["nombre"],
            "masked_email": _mask_email(session["email"]),
            "session_id": session_id,
            "modo": modo,
            "error": "Espera al menos 1 minuto antes de solicitar otro código.",
        })
    if not _rate_allow(f"send-otp:total:{session_id}", max_n=3, window_s=600):
        with _otp_lock:
            _otp_sessions.pop(session_id, None)
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Demasiados intentos de reenvío. Vuelve a ingresar tu documento.",
            "modo": modo,
        })

    # Generar código OTP
    code = _generate_otp_code()

    # Enviar por email
    success = await asyncio.to_thread(_send_otp_email, session["email"], code, session["nombre"])

    if not success:
        return templates.TemplateResponse(request=request, name="verify_send.html", context={
            "nombre": session["nombre"],
            "masked_email": _mask_email(session["email"]),
            "session_id": session_id,
            "modo": modo,
            "error": "No se pudo enviar el código. Intenta de nuevo.",
        })

    # Guardar código en sesión
    with _otp_lock:
        if session_id in _otp_sessions:
            _otp_sessions[session_id]["otp_code"] = code
            _otp_sessions[session_id]["otp_sent_at"] = time.time()

    # Mostrar pantalla para ingresar código
    return templates.TemplateResponse(request=request, name="verify_code.html", context={
        "nombre": session["nombre"],
        "masked_email": _mask_email(session["email"]),
        "session_id": session_id,
        "modo": modo,
        "error": None,
    })


@app.post("/verify-otp", response_class=HTMLResponse)
async def verify_otp(request: Request, session_id: str = Form(...), code: str = Form(...)):
    """Verifica el código OTP y redirige al destino según el modo."""
    with _otp_lock:
        session = _otp_sessions.get(session_id)

    if not session or time.time() - session.get("created", 0) > OTP_SESSION_MAX_AGE_SECONDS:
        return RedirectResponse(url="/", status_code=302)

    modo = session.get("modo", "web")
    code = code.strip()
    stored_code = session.get("otp_code", "")
    sent_at = session.get("otp_sent_at", 0)

    if not code or not stored_code:
        return templates.TemplateResponse(request=request, name="verify_code.html", context={
            "nombre": session["nombre"],
            "masked_email": _mask_email(session["email"]),
            "session_id": session_id,
            "modo": modo,
            "error": "Ingresa el código que recibiste por email.",
        })

    # ── Rate limiting: máx 5 intentos por sesión ─────────────────────────────
    if not _rate_allow(f"verify-otp:{session_id}", max_n=5, window_s=600):
        with _otp_lock:
            _otp_sessions.pop(session_id, None)
        return templates.TemplateResponse(request=request, name="login.html", context={
            "error": "Demasiados intentos incorrectos. Vuelve a ingresar tu documento.",
            "modo": modo,
        })

    # Verificar expiración (5 minutos)
    if time.time() - sent_at > 300:
        return templates.TemplateResponse(request=request, name="verify_code.html", context={
            "nombre": session["nombre"],
            "masked_email": _mask_email(session["email"]),
            "session_id": session_id,
            "modo": modo,
            "error": "El código ha expirado. Vuelve al inicio e intenta de nuevo.",
        })

    # Verificar código — comparación en tiempo constante (evita timing attacks)
    if not hmac.compare_digest(code, stored_code):
        return templates.TemplateResponse(request=request, name="verify_code.html", context={
            "nombre": session["nombre"],
            "masked_email": _mask_email(session["email"]),
            "session_id": session_id,
            "modo": modo,
            "error": "Código incorrecto. Verifica e intenta de nuevo.",
        })

    # ✓ Código válido — registrar el acceso antes de limpiar la sesión
    nombre = session["nombre"]
    cedula = session["cedula"]
    email  = session["email"]
    telefono = session.get("telefono", "")

    now = time.time()

    registrado = await asyncio.to_thread(
        _registrar_acceso_instalador,
        nombre,
        cedula,
        email,
        telefono,
        modo,
        _get_client_ip(request),
        request.headers.get("User-Agent", ""),
    )
    if not registrado:
        return templates.TemplateResponse(request=request, name="verify_code.html", context={
            "nombre": nombre,
            "masked_email": _mask_email(email),
            "session_id": session_id,
            "modo": modo,
            "error": "No pudimos registrar tu acceso. Intenta nuevamente en unos segundos.",
        })

    with _otp_lock:
        _otp_sessions.pop(session_id, None)

    if modo == "excel":
        # Generar copia en Google Sheets y redirigir
        try:
            copy_record = await asyncio.to_thread(
                create_catalog_copy,
                cedula=cedula,
                nombre=nombre,
            )
            catalog_link = copy_record["link"]
        except Exception:
            return templates.TemplateResponse(request=request, name="login.html", context={
                "error": "Error generando catálogo. Intenta de nuevo en unos segundos.",
                "modo": "excel",
            })

        token_excel = secrets.token_urlsafe(32)
        await asyncio.to_thread(_set_token, token_excel, {
            "type": "excel",
            "link": catalog_link,
            "file_id": copy_record.get("file_id", ""),
            "used": False,
            "created": now,
            "nombre": nombre,
            "cedula": cedula,
        })
        return RedirectResponse(url=f"/excel/{token_excel}", status_code=302)

    else:
        # modo == "web": token para el catálogo HTML
        token_web = secrets.token_urlsafe(32)
        await asyncio.to_thread(_set_token, token_web, {
            "type": "web",
            "used": False,
            "created": now,
            "nombre": nombre,
            "cedula": cedula,
        })
        return RedirectResponse(url=f"/ver/{token_web}", status_code=302)


@app.get("/ver/{token}")
async def ver_catalogo(request: Request, token: str):
    """Muestra el catálogo web HTML (token_web de un solo uso)."""
    data = await asyncio.to_thread(_get_token, token)

    # Fallbacks al entry point web (simetría con /excel)
    if data is None:
        return RedirectResponse(url="/web", status_code=302)
    if data.get("type") != "web":
        return RedirectResponse(url="/web", status_code=302)
    if time.time() - data.get("created", 0) > TOKEN_MAX_AGE_SECONDS:
        return RedirectResponse(url="/web", status_code=302)

    # Cargar datos del catálogo — desde caché JSON; fallback a MySQL si no existe
    catalog_data = await asyncio.to_thread(_read_cache_instalador)
    if catalog_data is None:
        catalog_data = await asyncio.to_thread(_load_catalog_data)

    return templates.TemplateResponse(request=request, name="catalogo_web.html", context={
        "fecha": catalog_data["fecha"],
        "nombre_cliente": data.get("nombre", ""),
        "categorias": catalog_data["categorias"],
        "productos_por_categoria": catalog_data["productos_por_categoria"],
    })


@app.get("/excel/{token}")
async def ver_excel(token: str):
    """Redirige al catálogo de Google Sheets (token_excel de un solo uso)."""
    data = await asyncio.to_thread(_get_token, token)

    # Fallbacks al ENTRY POINT del Excel (no a "/", que mostraría el web).
    # Esto evita que al retroceder con un token ya usado el cliente caiga en el web.
    if data is None:
        return RedirectResponse(url="/excel", status_code=302)
    if data.get("type") != "excel":
        return RedirectResponse(url="/excel", status_code=302)
    if data.get("used"):
        return RedirectResponse(url="/excel", status_code=302)
    if time.time() - data.get("created", 0) > TOKEN_MAX_AGE_SECONDS:
        return RedirectResponse(url="/excel", status_code=302)

    sheets_link = data.get("link", "")
    if not sheets_link:
        return RedirectResponse(url="/excel", status_code=302)

    # Marcar token como usado — la limpieza de Drive la hace el loop periódico
    data["used"] = True
    await asyncio.to_thread(_set_token, token, data)

    return RedirectResponse(url=sheets_link, status_code=302)


# Bodegas que cuentan como stock vendible. Se ignora cualquier otra
# (p. ej. "Baja de Mercancia", que es mercancía descartada y no debe
# sumar a la disponibilidad del catálogo).
BODEGAS_VALIDAS = {"principal", "bulevar", "caobos"}


def _calcular_disponibilidad_real(item: dict) -> int:
    """Disponibilidad real = disponible - remisionado, sumando SOLO las bodegas
    válidas (Principal, Bulevar, Caobos).

    Las unidades remisionadas están apartadas en remisiones abiertas, por lo que
    no están realmente disponibles. Bodegas como "Baja de Mercancia" se excluyen.
    """
    inventory = item.get("inventory", {})
    if not isinstance(inventory, dict):
        return 0

    warehouses = inventory.get("warehouses", []) or []
    if warehouses:
        total = 0
        for w in warehouses:
            if not isinstance(w, dict):
                continue
            nombre = str(w.get("name") or "").strip().lower()
            if nombre not in BODEGAS_VALIDAS:
                continue
            av = int(w.get("availableQuantity") or 0)
            rem = int(w.get("remissionedQuantity") or 0)
            total += (av - rem)
        return max(total, 0)

    # Fallback: sin detalle por bodega
    av = int(inventory.get("availableQuantity") or 0)
    rem = int(inventory.get("remissionedQuantity") or 0)
    return max(av - rem, 0)


def _load_catalog_data() -> dict:
    """
    Carga los productos del catálogo desde MySQL (inventario_local).

    Stock = suma de disponible en bodegas Principal (1), Caobos (2) y Bulevar (3).
    Precio final = precio_instalador * (1 + porcentaje_iva/100).
    Descuento por volumen = precio_aliados < precio_instalador.
    """
    from datetime import date

    # IDs de las bodegas válidas para el catálogo
    BODEGAS_CATALOGO = (1, 2, 3)  # Principal, Caobos, Bulevar

    try:
        conn   = mysql.connector.connect(**_DB_CONFIG)
        cursor = conn.cursor(dictionary=True)

        # Productos activos, no ocultos en tienda
        cursor.execute("""
            SELECT
                p.id_product,
                p.nombre,
                p.ref_producto   AS referencia,
                p.categoria,
                pi.precio AS precio_instalador,
                pa.precio AS precio_aliados,
                p.porcentaje_iva,
                p.ruta_imagen,
                COALESCE(
                    SUM(CASE WHEN s.id_warehouse IN (1,2,3) THEN s.disponible ELSE 0 END),
                    0
                ) AS stock_disponible
            FROM productos p
            LEFT JOIN stock s ON s.id_product = p.id_product
            LEFT JOIN al2.precio_producto pi
              ON pi.id_producto = p.id_product AND pi.id_lista_precio = 1
             AND pi.fecha_actualizacion = (
                 SELECT MAX(x.fecha_actualizacion) FROM al2.precio_producto x
                 WHERE x.id_producto = p.id_product AND x.id_lista_precio = 1
             )
            LEFT JOIN al2.precio_producto pa
              ON pa.id_producto = p.id_product AND pa.id_lista_precio = 3
             AND pa.fecha_actualizacion = (
                 SELECT MAX(x.fecha_actualizacion) FROM al2.precio_producto x
                 WHERE x.id_producto = p.id_product AND x.id_lista_precio = 3
             )
            WHERE p.estado = 'activo'
              AND p.ocultar_tienda = 0
            GROUP BY p.id_product, pi.precio, pa.precio
            ORDER BY p.categoria, LEFT(p.nombre, 1), pi.precio
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

    except Exception as e:
        # Si falla MySQL, log del error y devolver vacío (no rompe la app)
        print(f"[catalog] Error MySQL: {e}")
        rows = []

    productos = []
    for row in rows:
        precio_base = float(row["precio_instalador"] or 0)
        pct_iva     = float(row["porcentaje_iva"] or 0)
        precio_con_iva = round(precio_base * (1 + pct_iva / 100)) if precio_base > 0 else 0

        # Descuento por volumen: precio_aliados menor al instalador
        precio_aliados = float(row["precio_aliados"] or 0)
        dcto_volumen = bool(
            precio_aliados > 0 and precio_aliados < precio_base
        )

        precio_formato = f"${precio_con_iva:,.0f}" if precio_con_iva > 10 else "CONTACTAR"
        categoria = (row["categoria"] or "OTROS").upper()

        productos.append({
            "id":           str(row["id_product"]),
            "nombre":       row["nombre"] or "",
            "referencia":   row["referencia"] or "",
            "categoria":    categoria,
            "precio_con_iva": precio_con_iva,
            "precio_formato": precio_formato,
            "stock":        int(row["stock_disponible"] or 0),
            "dcto_volumen": dcto_volumen,
            "imagen_url":   row["ruta_imagen"] or "",
        })

    # Agrupar por categoría
    categorias_dict: dict = {}
    for p in productos:
        cat = p["categoria"]
        if cat not in categorias_dict:
            categorias_dict[cat] = []
        categorias_dict[cat].append(p)

    return {
        "fecha": date.today().strftime("%d/%m/%Y"),
        "categorias": sorted(categorias_dict.keys()),
        "productos_por_categoria": categorias_dict,
    }


# ═══════════ Catálogo para clientes finales (precio CLIENTE) ═══════════

def _load_catalog_data_cliente() -> dict:
    """
    Carga los productos con precio CLIENTE para el catálogo público.

    Estrategia dual:
    1. Intenta leer desde MySQL (cuando está disponible — entorno local).
    2. Si MySQL falla, lee directamente desde data/items.json (producción sin MySQL).
    """
    from datetime import date

    rows = None  # None = intentar MySQL; [] = MySQL falló

    try:
        conn   = mysql.connector.connect(**_DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT
                p.id_product,
                p.nombre,
                p.ref_producto   AS referencia,
                p.categoria,
                pc.precio AS precio_cliente,
                p.porcentaje_iva,
                p.ruta_imagen,
                COALESCE(
                    SUM(CASE WHEN s.id_warehouse IN (1,2,3) THEN s.disponible ELSE 0 END),
                    0
                ) AS stock_disponible
            FROM productos p
            LEFT JOIN stock s ON s.id_product = p.id_product
            LEFT JOIN al2.precio_producto pc
              ON pc.id_producto = p.id_product AND pc.id_lista_precio = 2
             AND pc.fecha_actualizacion = (
                 SELECT MAX(x.fecha_actualizacion) FROM al2.precio_producto x
                 WHERE x.id_producto = p.id_product AND x.id_lista_precio = 2
             )
            WHERE p.estado = 'activo'
              AND p.ocultar_tienda = 0
            GROUP BY p.id_product, pc.precio
            ORDER BY p.categoria, p.nombre
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[catalog_cliente] MySQL no disponible ({e}), usando items.json...")
        rows = None

    # ── Fuente MySQL disponible ──────────────────────────────────────────
    if rows is not None:
        productos = []
        for row in rows:
            precio_base    = float(row["precio_cliente"] or 0)
            pct_iva        = float(row["porcentaje_iva"] or 0)
            precio_con_iva = round(precio_base * (1 + pct_iva / 100)) if precio_base > 0 else 0
            precio_formato = f"${precio_con_iva:,.0f}" if precio_con_iva > 10 else "CONTACTAR"
            categoria      = (row["categoria"] or "OTROS").upper()
            productos.append({
                "id":             str(row["id_product"]),
                "nombre":         row["nombre"] or "",
                "referencia":     row["referencia"] or "",
                "categoria":      categoria,
                "precio_con_iva": precio_con_iva,
                "precio_formato": precio_formato,
                "stock":          int(row["stock_disponible"] or 0),
                "imagen_url":     row["ruta_imagen"] or "",
            })

    # ── Fallback: leer items.json (producción sin MySQL) ─────────────────
    else:
        items_path = PROJECT_ROOT / "data" / "items.json"
        try:
            with open(items_path, encoding="utf-8") as f:
                items_raw = json.load(f)
        except Exception as e:
            print(f"[catalog_cliente] No se pudo leer items.json: {e}")
            items_raw = []

        # Cargar URLs de Cloudinary
        urls_path = PROJECT_ROOT / "data" / "images_urls.json"
        images_urls: dict = {}
        try:
            with open(urls_path, encoding="utf-8") as f:
                images_urls = json.load(f)
        except Exception:
            pass

        productos = []
        for item in items_raw:
            # Filtros equivalentes a MySQL: activo, no oculto, tipo product
            if item.get("status") != "active":
                continue
            if item.get("type") != "product":
                continue
            settings = item.get("settingsOnShop") or {}
            if settings.get("hide") is True:
                continue

            # Precio CLIENTE (idPriceList=2)
            precio_base = 0.0
            for p in (item.get("price") or []):
                if p.get("name") == "CLIENTE":
                    v = p.get("price")
                    if v:
                        precio_base = float(v)
                    break

            # IVA
            pct_iva = 0.0
            for t in (item.get("tax") or []):
                if t.get("type") == "IVA" and t.get("status") == "active":
                    pct_iva = float(t.get("percentage") or 0)
                    break

            precio_con_iva = round(precio_base * (1 + pct_iva / 100)) if precio_base > 0 else 0
            precio_formato = f"${precio_con_iva:,.0f}" if precio_con_iva > 10 else "CONTACTAR"

            # Categoría
            cat_obj  = item.get("itemCategory") or {}
            categoria = (cat_obj.get("name") or "OTROS").upper()

            # Stock (solo bodegas válidas)
            stock = _calcular_disponibilidad_real(item)

            # Imagen desde Cloudinary
            imagen_url = images_urls.get(str(item.get("id", "")), "")

            productos.append({
                "id":             str(item.get("id", "")),
                "nombre":         item.get("name", ""),
                "referencia":     item.get("reference", "") or "",
                "categoria":      categoria,
                "precio_con_iva": precio_con_iva,
                "precio_formato": precio_formato,
                "stock":          stock,
                "imagen_url":     imagen_url,
            })

        # Ordenar igual que MySQL: categoría → nombre
        productos.sort(key=lambda x: (x["categoria"], x["nombre"]))

    # Agrupar por categoría
    categorias_dict: dict = {}
    for p in productos:
        cat = p["categoria"]
        if cat not in categorias_dict:
            categorias_dict[cat] = []
        categorias_dict[cat].append(p)

    return {
        "fecha":                   date.today().strftime("%d/%m/%Y"),
        "categorias":              sorted(categorias_dict.keys()),
        "productos_por_categoria": categorias_dict,
    }


# ═══════════ Catálogo Público — clientes finales (/publico) ═══════════

# Nombre de la cookie que guarda el teléfono del cliente entre sesiones
_COOKIE_CLIENTE = "alianxa_cliente"
# Tiempo de vida de la cookie: 1 año en segundos
_COOKIE_MAX_AGE = 365 * 24 * 3600


def _get_client_ip(request: Request) -> str:
    """Extrae la IP real del cliente, considerando proxies/ngrok."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.get("/publico", response_class=HTMLResponse)
async def login_publico(
    request: Request,
    alianxa_cliente: str | None = Cookie(default=None),
):
    """Entry point del catálogo público.

    - Si el navegador tiene la cookie con el teléfono → ingreso directo sin formulario.
    - Si no → muestra el formulario de nombre + teléfono.
    """
    if alianxa_cliente:
        # Verificar que el teléfono existe en BD
        cliente = await asyncio.to_thread(_buscar_cliente_por_telefono, alianxa_cliente)
        if cliente:
            # Registrar ingreso en otp_log (modo=publico) y redirigir directo
            await asyncio.gather(
                asyncio.to_thread(
                    _log_otp_success,
                    cliente["telefono"],
                    cliente["nombre"],
                    "publico",
                ),
                asyncio.to_thread(
                    _log_acceso_catalogo,
                    cliente["nombre"],
                    None,
                    None,
                    cliente["telefono"],
                    "publico",
                    "cliente_publico",
                    False,
                    _get_client_ip(request),
                    request.headers.get("User-Agent", ""),
                ),
            )

            token = secrets.token_urlsafe(32)
            await asyncio.to_thread(_set_token, token, {
                "type": "cliente",
                "used": False,
                "created": time.time(),
                "nombre": cliente["nombre"],
            })
            return RedirectResponse(url=f"/ver-publico/{token}", status_code=302)

    # Sin cookie o teléfono no encontrado → mostrar formulario
    return templates.TemplateResponse(
        request=request,
        name="login_publico.html",
        context={"error": None},
    )


@app.post("/publico/ingresar", response_class=HTMLResponse)
async def ingresar_publico(
    request: Request,
    nombre: str = Form(...),
    telefono: str = Form(...),
):
    """Valida nombre+teléfono, registra/actualiza en BD, pone cookie y genera token."""
    import re

    nombre   = nombre.strip()[:200]
    telefono = re.sub(r"\D", "", telefono.strip())[:50]

    if len(nombre) < 2:
        return templates.TemplateResponse(
            request=request,
            name="login_publico.html",
            context={"error": "Por favor ingresa tu nombre completo."},
        )
    if len(telefono) < 7:
        return templates.TemplateResponse(
            request=request,
            name="login_publico.html",
            context={"error": "Ingresa un número de teléfono válido (mínimo 7 dígitos)."},
        )

    ip = _get_client_ip(request)
    ua = request.headers.get("User-Agent", "")

    # Registrar o actualizar cliente en visitas_clientes (una fila por teléfono)
    await asyncio.to_thread(_log_visita_cliente, nombre, telefono, ip, ua)

    # Registrar el ingreso en otp_log (modo=publico) — misma tabla que instaladores
    await asyncio.gather(
        asyncio.to_thread(_log_otp_success, telefono, nombre, "publico"),
        asyncio.to_thread(
            _log_acceso_catalogo,
            nombre,
            None,
            None,
            telefono,
            "publico",
            "cliente_publico",
            False,
            ip,
            ua,
        ),
    )

    # Generar token de sesión (5 minutos, un solo uso)
    token = secrets.token_urlsafe(32)
    await asyncio.to_thread(_set_token, token, {
        "type": "cliente",
        "used": False,
        "created": time.time(),
        "nombre": nombre,
    })

    # Redirigir con cookie persistente (1 año)
    response = RedirectResponse(url=f"/ver-publico/{token}", status_code=302)
    response.set_cookie(
        key=_COOKIE_CLIENTE,
        value=telefono,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return response


@app.get("/ver-publico/{token}", response_class=HTMLResponse)
async def ver_catalogo_publico(request: Request, token: str):
    """Muestra el catálogo HTML con precios CLIENTE (token de un solo uso, 5 min)."""
    start = time.time()
    # Usar lectura no destructiva para evitar que prefetches/requests
    # auxiliares consuman el token antes de que la navegación completa se
    # realice. El token se consumirá explícitamente si es necesario en
    # acciones posteriores.
    data = await asyncio.to_thread(_peek_token, token)

    if data is None or data.get("type") != "cliente":
        return RedirectResponse(url="/publico", status_code=302)
    if time.time() - data.get("created", 0) > 300:   # 5 minutos
        return RedirectResponse(url="/publico", status_code=302)

    # Medir tiempos de lectura/compilación del catálogo
    t0 = time.time()
    catalog_data = await asyncio.to_thread(_read_cache_cliente)
    t_cache_read = time.time() - t0

    if catalog_data is None:
        t1 = time.time()
        catalog_data = await asyncio.to_thread(_load_catalog_data_cliente)
        t_cache_build = time.time() - t1
    else:
        t_cache_build = 0.0

    # HTML pre-renderizado — si no existe, generarlo ahora (primera vez en servidor)
    t2 = time.time()
    catalog_rows = await asyncio.to_thread(_read_html_cliente)
    t_html_read = time.time() - t2
    t_html_build = 0.0
    if not catalog_rows:
        from src.catalog_cache import build_html_cliente
        t3 = time.time()
        catalog_rows = await asyncio.to_thread(build_html_cliente, catalog_data)
        t_html_build = time.time() - t3

    total = time.time() - start
    # Registrar en logs para diagnóstico
    print(f"[ver-publico] token={token[:8]}.. nombre={data.get('nombre','?')[:20]} timings: cache_read={t_cache_read:.3f}s cache_build={t_cache_build:.3f}s html_read={t_html_read:.3f}s html_build={t_html_build:.3f}s total={total:.3f}s")

    response = templates.TemplateResponse(
        request=request,
        name="catalogo_cliente.html",
        context={
            "fecha":                   catalog_data["fecha"],
            "nombre_cliente":          data.get("nombre", ""),
            "token":                   token,
            "categorias":              catalog_data["categorias"],
            "catalog_rows":            catalog_rows,
            "productos_por_categoria": {},
        },
    )
    # Añadir cabeceras de diagnóstico para medir en el cliente
    response.headers["X-Timing-Total"] = f"{total:.3f}"
    response.headers["X-Timing-Cache-Read"] = f"{t_cache_read:.3f}"
    response.headers["X-Timing-Cache-Build"] = f"{t_cache_build:.3f}"
    response.headers["X-Timing-HTML-Read"] = f"{t_html_read:.3f}"
    response.headers["X-Timing-HTML-Build"] = f"{t_html_build:.3f}"
    return response


@app.post("/consume-token/{token}")
async def consume_token_endpoint(token: str):
    """Endpoint sencillo que consume (elimina) un token cuando el cliente
    confirma que el HTML se cargó correctamente.
    """
    data = await asyncio.to_thread(_consume_token, token)
    if data is None:
        return Response(status_code=404, content="")
    return Response(status_code=204, content="")
