"""
Script principal para actualización automática del catálogo.
Se ejecuta como tarea programada (ej: 3:00 AM diariamente).

Flujo completo (7 pasos):
1. Extraer todos los items de Alegra (activos + inactivos)
2. Descargar imágenes nuevas/actualizadas
3. Subir imágenes a Cloudinary
4. Sincronizar MySQL + regenerar caché web
5. Regenerar maestro Google Spreadsheet
6. Generar inventario por bodega desde items.json y subir a Drive
7. Completado

Flujo sin imágenes --skip-images (5 pasos):
1. Extraer todos los items de Alegra (activos + inactivos)
2. Sincronizar MySQL + regenerar caché web
3. Regenerar maestro Google Spreadsheet
4. Generar inventario por bodega desde items.json y subir a Drive
5. Completado

Uso manual:
    py actualizar_catalogo.py               # flujo completo
    py actualizar_catalogo.py --skip-images # omite descarga y Cloudinary
"""

import argparse
import atexit
import json
import os
import smtplib
import sys
import time
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Asegurar que estamos en el directorio correcto
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from src.config import validate_config
from src.items import get_all_items_with_inactive, save_items_to_json
from src.download_images import download_all_images

LOCK_FILE       = "data/.actualizar_catalogo.lock"
RUN_STATUS_FILE = "data/run_status.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _release_run_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def _acquire_run_lock(lock_path: str, wait: bool = False) -> None:
    """Evita dos actualizaciones simultáneas y recupera locks abandonados.

    Libera automáticamente locks huérfanos de más de 2 horas (por si el
    contenedor Docker se reinició y dejó el lock en disco con un PID stale).
    Si wait=True, espera a que termine el proceso que tiene el lock.
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    waiting_logged = False
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Guard de antigüedad: lock de más de 2 horas = proceso muerto/reinicio
            try:
                age = time.time() - os.stat(lock_path).st_mtime
                if age > 7200:
                    print(f"  ⚠ Lock huérfano detectado (antigüedad: {age/3600:.1f}h) — liberando.")
                    _release_run_lock(lock_path)
                    continue
            except FileNotFoundError:
                continue

            try:
                with open(lock_path, encoding="ascii") as lock_file:
                    old_pid = int(lock_file.read().strip())
                os.kill(old_pid, 0)
            except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
                _release_run_lock(lock_path)
                continue

            if wait:
                if not waiting_logged:
                    print(
                        f"  Hay una actualización en ejecución (PID {old_pid}); "
                        "esperando a que termine...",
                        flush=True,
                    )
                    waiting_logged = True
                time.sleep(10)
                continue
            raise RuntimeError(
                f"Ya existe una actualización en ejecución (PID {old_pid})."
            )

        with os.fdopen(fd, "w", encoding="ascii") as lock_file:
            lock_file.write(str(os.getpid()))
        atexit.register(_release_run_lock, lock_path)
        return


def log(msg):
    """Imprime con timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def _write_run_status(start: float, end: float, ok: bool, last_step: str, error: str = ""):
    """Escribe data/run_status.json con el resultado de la corrida (atómico).
    
    Estructura:
      start, end, duration_s, ok, last_step, error
    Permite saber sin leer el log si la última corrida fue exitosa.
    """
    status = {
        "start":      datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S"),
        "end":        datetime.fromtimestamp(end).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(end - start),
        "ok":         ok,
        "last_step":  last_step,
        "error":      error,
    }
    tmp = RUN_STATUS_FILE + ".tmp"
    os.makedirs(os.path.dirname(RUN_STATUS_FILE), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RUN_STATUS_FILE)


def _send_alert(subject: str, body: str) -> None:
    """Envía alerta por Gmail SMTP cuando una corrida falla.
    Reutiliza las mismas credenciales del OTP del catálogo web.
    No lanza excepción — si falla el correo, solo lo registra en el log.
    """
    smtp_email = os.getenv("SMTP_EMAIL", "")
    smtp_pass  = os.getenv("SMTP_APP_PASSWORD", "")
    if not smtp_email or not smtp_pass:
        print("  ⚠ SMTP no configurado — no se puede enviar alerta por correo.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Catálogo Alianxa <{smtp_email}>"
        msg["To"]      = smtp_email  # alerta interna al mismo correo configurado
        html = f"""
        <div style="font-family:Arial,sans-serif;padding:20px;color:#333;">
          <h2 style="color:#C62828;">⚠ Alerta — Catálogo Alianxa</h2>
          <pre style="background:#f5f5f5;padding:12px;border-radius:4px;
                      font-size:13px;white-space:pre-wrap;">{body}</pre>
          <p style="color:#888;font-size:12px;">
            Servidor: {os.getenv("NGROK_DOMAIN", "servidor")}
          </p>
        </div>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_pass)
            server.sendmail(smtp_email, smtp_email, msg.as_string())
        print(f"  ✓ Alerta enviada a {smtp_email}")
    except Exception as e:
        print(f"  ⚠ No se pudo enviar la alerta por correo: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Actualización automática del catálogo")
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Omite descarga de imágenes (Paso 2) y subida a Cloudinary (Paso 3)",
    )
    parser.add_argument(
        "--wait-for-lock",
        action="store_true",
        help="Espera a que termine otra actualización antes de iniciar",
    )
    args = parser.parse_args()

    # Número total de pasos según el modo
    total_pasos = 4 if args.skip_images else 6

    _acquire_run_lock(LOCK_FILE, wait=args.wait_for_lock)
    start_time = time.time()
    last_step  = "inicio"

    log("═" * 50)
    log("ACTUALIZACIÓN AUTOMÁTICA DE CATÁLOGO")
    if args.skip_images:
        log("  Modo: sin imágenes (--skip-images)")
    else:
        log("  Modo: flujo completo")
    log("═" * 50)

    try:
        # Validar credenciales
        validate_config()

        # Crear carpetas necesarias
        os.makedirs("data", exist_ok=True)
        os.makedirs("data/images", exist_ok=True)

        # ── Paso 1: Extraer items de Alegra ───────────────────────────────────
        last_step = f"Paso 1/{total_pasos}: Extracción de Alegra"
        log(f"{last_step}...")
        items = get_all_items_with_inactive()

        if not items:
            log("ERROR: No se pudieron obtener items. Abortando.")
            sys.exit(1)

        save_items_to_json(items, "data/items.json")
        activos   = sum(1 for i in items if i.get("status") == "active")
        inactivos = sum(1 for i in items if i.get("status") == "inactive")
        log(f"  → {len(items)} items extraídos ({activos} activos, {inactivos} inactivos)")

        if not args.skip_images:
            # ── Paso 2: Descargar imágenes ────────────────────────────────────
            last_step = f"Paso 2/{total_pasos}: Descarga de imágenes"
            log(f"{last_step}...")
            download_all_images(solo_con_stock=False)

            # ── Paso 3: Subir imágenes a Cloudinary ───────────────────────────
            last_step = f"Paso 3/{total_pasos}: Upload a Cloudinary"
            log(f"{last_step}...")
            from src.upload_cloudinary import main as upload_cloudinary
            upload_cloudinary()

        # ── Paso 4 (ó 2): Sincronizar MySQL + caché web ───────────────────────
        paso_mysql = 4 if not args.skip_images else 2
        last_step = f"Paso {paso_mysql}/{total_pasos}: Sincronización MySQL"
        log(f"{last_step}...")
        from src.database import sync_from_json
        resumen_sync = sync_from_json(
            items_path="data/items.json",
            images_path="data/images_urls.json",
        )
        if resumen_sync["errores"] > 0:
            log(
                f"ERROR: la sincronización MySQL terminó con "
                f"{resumen_sync['errores']} errores. Se detiene la publicación."
            )
            sys.exit(1)

        log("Sincronizando precios y stock histórico de AL2...")
        from src.al2_sync import sync_al2
        resumen_al2 = sync_al2(items)
        log(
            f"  → AL2: {resumen_al2['precios']} precios, "
            f"{resumen_al2['stock']} registros de stock, "
            f"{resumen_al2['omitidos']} items omitidos por no existir en AL2.products, "
            f"timestamp {resumen_al2['timestamp']}"
        )
        if resumen_al2["errores"] > 0:
            log(
                f"ERROR: la sincronización AL2 terminó con "
                f"{resumen_al2['errores']} errores. Se detiene la publicación."
            )
            sys.exit(1)

        log("Regenerando caché de catálogos...")
        from src.catalog_cache import rebuild_all
        rebuild_all()

        # ── Paso 5 (ó 3): Regenerar maestro Google Spreadsheet ───────────────
        paso_sheets = 5 if not args.skip_images else 3
        last_step = f"Paso {paso_sheets}/{total_pasos}: Maestro Google Sheets"
        log(f"{last_step}...")
        from proyecto_ecxel.share_catalog import generate_catalog_xlsx
        generate_catalog_xlsx()

        log("Limpiando copias expiradas...")
        from proyecto_ecxel.share_catalog import cleanup_expired_copies
        cleanup_expired_copies()

        # El inventario por bodegas se actualiza exclusivamente a las 08:00,
        # 15:00 y 18:00 mediante excel_ai.inventario_bodegas. No se regenera
        # aquí para evitar sobrescribir el mismo archivo de Drive.

        # ── Paso final: Completado ────────────────────────────────────────────
        last_step = f"Paso {total_pasos}/{total_pasos}: Completado"
        end_time  = time.time()
        duracion  = round(end_time - start_time)
        log("═" * 50)
        log(f"✓ ACTUALIZACIÓN COMPLETADA en {duracion}s ({duracion//60}m {duracion%60}s)")
        log("═" * 50)
        _write_run_status(start_time, end_time, ok=True, last_step=last_step)

    except SystemExit as e:
        # sys.exit(1) desde un paso — ya logueado arriba
        end_time = time.time()
        _write_run_status(start_time, end_time, ok=False,
                          last_step=last_step, error=f"sys.exit({e.code})")
        if str(e.code) != "0":
            _send_alert(
                subject=f"⚠ Catálogo Alianxa — Fallo en {last_step}",
                body=(
                    f"Paso fallido: {last_step}\n"
                    f"Código de salida: {e.code}\n"
                    f"Duración hasta el fallo: {round(time.time()-start_time)}s\n\n"
                    "Revisa data/cron_catalogo.log para el detalle."
                ),
            )
        raise

    except Exception as e:
        # Error inesperado no capturado
        end_time = time.time()
        error = f"{type(e).__name__}: {str(e) or repr(e)}"
        log(f"ERROR INESPERADO en {last_step}: {error}")
        log(traceback.format_exc().rstrip())
        _write_run_status(start_time, end_time, ok=False,
                          last_step=last_step, error=error)
        _send_alert(
            subject=f"⚠ Catálogo Alianxa — Error inesperado en {last_step}",
            body=(
                f"Paso: {last_step}\n"
                f"Error: {error}\n"
                f"Duración hasta el fallo: {round(time.time()-start_time)}s\n\n"
                "Revisa data/cron_catalogo.log para el traceback completo."
            ),
        )
        raise


if __name__ == "__main__":
    main()
