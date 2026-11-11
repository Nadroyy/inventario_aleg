"""
Módulo de sincronización de items de Alegra → MySQL.

Soporta el esquema real en uso en el sistema actual:
  - productos
  - warehouses
  - stock

También mantiene compatibilidad con el patrón antiguo de `sync_log` si la tabla
existe, pero no exige que exista para que la sincronización siga funcionando.
"""

import json
import os
import time
from datetime import datetime

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error

_RETRYABLE_SYNC_ERROR_CODES = {1205, 1213}
_MAX_SYNC_RETRIES = 3

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "inventario_local"),
    "charset": "utf8mb4",
    "autocommit": False,
}


def get_connection():
    """Retorna una conexión activa a MySQL."""
    return mysql.connector.connect(**DB_CONFIG)


def _get_stock_total(item: dict) -> float:
    """Devuelve el stock total disponible del item."""
    inv = item.get("inventory") or {}
    return float(inv.get("availableQuantity") or 0)


def _get_iva(item: dict) -> tuple[int, float | None]:
    """Retorna (tiene_iva, porcentaje) del primer IVA activo."""
    for tax in item.get("tax") or []:
        if str(tax.get("type", "")).upper() == "IVA" and str(tax.get("status", "")).lower() == "active":
            return 1, float(tax.get("percentage") or 0)
    return 0, None


def _get_imagen_url(item_id: str, images_urls: dict) -> str | None:
    """Busca la URL de Cloudinary para el item."""
    return images_urls.get(str(item_id))


def _get_categoria(item: dict) -> tuple[int | None, str | None]:
    cat = item.get("itemCategory") or {}
    return cat.get("id"), cat.get("name")


def _estado_real(value: str | None) -> str:
    estado = str(value or "active").strip().lower()
    return "activo" if estado in {"active", "activo"} else "inactivo"


def _get_precio_leyendo_nombre(item: dict, nombres: set[str]) -> float | None:
    for p in item.get("price") or []:
        nombre = str(p.get("name") or "").strip().upper()
        if nombre in nombres:
            valor = p.get("price")
            try:
                return float(valor) if valor is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _upsert_item(cursor, item: dict, images_urls: dict) -> str:
    """
    Inserta o actualiza un item en la tabla real `productos` y su stock/warehouses.
    Retorna 'insertado' o 'actualizado'.
    """
    item_id = str(item["id"])
    nombre = (item.get("name") or "")[:150] or f"Producto {item_id}"
    referencia = (item.get("reference") or "").strip()[:50] or item_id[:50] or "SIN_REFERENCIA"
    descripcion = item.get("description") or None
    estado = _estado_real(item.get("status", "active"))
    _, cat_nombre = _get_categoria(item)
    ocultar_tienda = 1 if bool((item.get("settingsOnShop") or {}).get("hide")) else 0
    _, pct_iva = _get_iva(item)
    stock_total = _get_stock_total(item)
    imagen_url = _get_imagen_url(item_id, images_urls)
    moneda = "COP"
    for p in item.get("price") or []:
        moneda = str((p.get("currency") or {}).get("code") or moneda).upper()
        break

    escala_descuentos = None
    custom_fields = item.get("customFields") or item.get("custom_fields") or {}
    if isinstance(custom_fields, dict):
        escala_descuentos = custom_fields.get("escala_descuentos") or custom_fields.get("escalaDescuentos")
    if escala_descuentos is None:
        escala_descuentos = None

    sql_item = """
        INSERT INTO productos
            (id_product, ref_producto, nombre, tipo, descripcion, estado,
             categoria, ocultar_tienda, escala_descuentos, ruta_imagen,
             moneda, porcentaje_iva, fecha_actualizacion)
        VALUES
            (%s, %s, %s, %s, %s, %s,
             %s, %s, %s, %s,
             %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            ref_producto        = VALUES(ref_producto),
            nombre              = VALUES(nombre),
            tipo                = VALUES(tipo),
            descripcion         = VALUES(descripcion),
            estado              = VALUES(estado),
            categoria           = VALUES(categoria),
            ocultar_tienda      = VALUES(ocultar_tienda),
            escala_descuentos   = VALUES(escala_descuentos),
            ruta_imagen         = VALUES(ruta_imagen),
            moneda              = VALUES(moneda),
            porcentaje_iva      = VALUES(porcentaje_iva),
            fecha_actualizacion = NOW()
    """
    cursor.execute(
        sql_item,
        (
            item_id,
            referencia,
            nombre,
            (item.get("type") or "product")[:50],
            descripcion,
            estado,
            (cat_nombre or "")[:100] if cat_nombre else None,
            ocultar_tienda,
            json.dumps(escala_descuentos, ensure_ascii=False) if escala_descuentos is not None else None,
            imagen_url,
            moneda,
            pct_iva,
        ),
    )
    accion = "insertado" if cursor.rowcount == 1 else "actualizado"

    cursor.execute("DELETE FROM stock WHERE id_product = %s", (item_id,))

    inv = item.get("inventory") or {}
    for w in inv.get("warehouses") or []:
        warehouse_id = str(w.get("id") or "")
        warehouse_name = (w.get("name") or "Bodega")[:45]
        warehouse_address = (w.get("address") or "Sin dirección")[:100]

        cursor.execute(
            """
            INSERT INTO warehouses (id_warehouse, name, address)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                address = VALUES(address)
            """,
            (warehouse_id, warehouse_name, warehouse_address),
        )

        # Disponible real = available − remisionado (unidades comprometidas en remisiones abiertas)
        av  = float(w.get("availableQuantity")   or 0)
        rem = float(w.get("remissionedQuantity") or 0)
        stock_disponible = max(av - rem, 0)
        stock_minimo = float(w.get("minQuantity") or 0)
        cursor.execute(
            """
            INSERT INTO stock (id_product, id_warehouse, disponible, stock_minimo)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                disponible = VALUES(disponible),
                stock_minimo = VALUES(stock_minimo)
            """,
            (item_id, warehouse_id, stock_disponible, stock_minimo),
        )

    return accion


def sync_items(items: list[dict], images_urls: dict | None = None) -> dict:
    """
    Sincroniza una lista de items al esquema real del sistema (`productos`/`stock`).
    """
    if images_urls is None:
        images_urls = {}

    resumen = {"total": len(items), "insertados": 0, "actualizados": 0, "errores": 0}
    inicio = datetime.now()

    try:
        conn = get_connection()
        cursor = conn.cursor()
    except Error as e:
        print(f"✗ No se pudo conectar a MySQL: {e}")
        resumen["errores"] = len(items) or 1
        return resumen

    sync_log_exists = False
    try:
        cursor.execute("SHOW TABLES LIKE 'sync_log'")
        sync_log_exists = cursor.fetchone() is not None
    except Exception:
        sync_log_exists = False

    log_id = None
    if sync_log_exists:
        cursor.execute(
            "INSERT INTO sync_log (fecha_inicio, total_items, estado) VALUES (%s, %s, 'en_progreso')",
            (inicio, len(items)),
        )
        log_id = cursor.lastrowid
        conn.commit()

    print(f"  Sincronizando {len(items)} items a MySQL...")

    # Batch commits cada COMMIT_BATCH items para reducir round-trips WAN.
    # Con MySQL remoto, commit por item tardaba ~50 min.
    # Con batch=50 el tiempo baja a ~5-8 min.
    COMMIT_BATCH = 50
    items_en_batch = 0

    for i, item in enumerate(items, 1):
        for intento in range(_MAX_SYNC_RETRIES + 1):
            try:
                accion = _upsert_item(cursor, item, images_urls)
                resumen[f"{accion}s"] += 1
                items_en_batch += 1
                # Commit cada COMMIT_BATCH items o al llegar al último
                if items_en_batch >= COMMIT_BATCH or i == len(items):
                    conn.commit()
                    items_en_batch = 0
                if i % 100 == 0:
                    print(f"  → {i}/{len(items)} procesados...")
                break
            except Error as e:
                conn.rollback()
                items_en_batch = 0
                if e.errno in _RETRYABLE_SYNC_ERROR_CODES and intento < _MAX_SYNC_RETRIES:
                    espera = 0.5 * (2 ** intento)
                    print(
                        f"  ⚠ Error MySQL {e.errno} en item {item.get('id', '?')}; "
                        f"reintento {intento + 1}/{_MAX_SYNC_RETRIES} en {espera:.1f}s"
                    )
                    time.sleep(espera)
                    continue
                resumen["errores"] += 1
                print(f"  ⚠ Error en item {item.get('id', '?')}: {e}")
                break
            except Exception as e:
                conn.rollback()
                items_en_batch = 0
                resumen["errores"] += 1
                print(f"  ⚠ Error en item {item.get('id', '?')}: {e}")
                break

    fin = datetime.now()
    duracion = (fin - inicio).total_seconds()

    if sync_log_exists and log_id is not None:
        cursor.execute(
            """UPDATE sync_log
               SET fecha_fin = %s, insertados = %s, actualizados = %s,
                   errores = %s, estado = %s, detalle = %s
               WHERE id = %s""",
            (
                fin,
                resumen["insertados"],
                resumen["actualizados"],
                resumen["errores"],
                "ok" if resumen["errores"] == 0 else "error_parcial",
                f"Duración: {duracion:.1f}s",
                log_id,
            ),
        )
        conn.commit()

    cursor.close()
    conn.close()

    print(
        f"  OK Sync completado en {duracion:.1f}s - "
        f"insertados: {resumen['insertados']}, "
        f"actualizados: {resumen['actualizados']}, "
        f"errores: {resumen['errores']}"
    )
    return resumen


def sync_from_json(
    items_path: str = "data/items.json",
    images_path: str = "data/images_urls.json",
) -> dict:
    """Carga JSON de disco y sincroniza con la base real del sistema."""
    with open(items_path, encoding="utf-8") as f:
        items = json.load(f)

    images_urls = {}
    if os.path.exists(images_path):
        with open(images_path, encoding="utf-8") as f:
            images_urls = json.load(f)

    return sync_items(items, images_urls)


def sync_precios_cliente(
    items: list[dict] | None = None,
    items_path: str = "data/items.json",
) -> dict:
    """Compatibilidad histórica; los precios ahora viven en ``al2``."""
    if items is None:
        with open(items_path, encoding="utf-8") as f:
            items = json.load(f)

    resumen = {"total": len(items), "actualizados": 0, "sin_precio": 0, "errores": 0}
    print("  ✓ Los precios se sincronizan en al2.precio_producto; no se modifica productos.")
    return resumen
