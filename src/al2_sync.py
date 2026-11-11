"""Sincronización de precios e inventario de Alegra hacia la base AL2."""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

_BOGOTA = ZoneInfo("America/Bogota")
_WAREHOUSES_FOR_CATALOG = {"1", "2", "3"}
_PRICE_LIST_IDS = {"1", "2", "3", "4"}


def _db_config() -> dict:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": "al2",
        "charset": "utf8mb4",
        "autocommit": False,
        "connection_timeout": 15,
    }


def _iva_percentage(item: dict) -> Decimal:
    for tax in item.get("tax") or []:
        if (
            str(tax.get("type", "")).upper() == "IVA"
            and str(tax.get("status", "")).lower() == "active"
        ):
            try:
                return Decimal(str(tax.get("percentage") or 0))
            except Exception:
                return Decimal("0")
    return Decimal("0")


def price_with_iva(price: object, percentage: Decimal) -> Decimal:
    """Calcula el precio final con IVA y redondea a dos decimales."""
    value = Decimal(str(price or 0))
    result = value * (Decimal("1") + percentage / Decimal("100"))
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def execution_timestamp() -> tuple[int, str]:
    """Devuelve timestamp Unix y fecha legible de la ejecución en Colombia."""
    now = datetime.now(_BOGOTA)
    return int(now.timestamp()), now.isoformat(timespec="seconds")


def _sync_runs_available(cursor) -> bool:
    cursor.execute("SHOW TABLES LIKE 'sync_runs'")
    return cursor.fetchone() is not None


def sync_al2(items: list[dict]) -> dict:
    """Agrega el estado de precios y stock de una ejecución a AL2.

    Las tablas de AL2 usan la fecha como parte de su índice no repetible,
    por lo que cada ejecución conserva una fotografía histórica.
    """
    timestamp, started_colombia = execution_timestamp()
    result = {
        "timestamp": timestamp,
        "items": len(items),
        "precios": 0,
        "stock": 0,
        "errores": 0,
        "omitidos": 0,
    }
    conn = mysql.connector.connect(**_db_config())
    cursor = conn.cursor()
    try:
        sync_runs_available = _sync_runs_available(cursor)
        run_id = None
        if sync_runs_available:
            cursor.execute(
                """
                INSERT INTO sync_runs
                    (timestamp_inicio, fecha_inicio_colombia, total_items, estado)
                VALUES (%s, %s, %s, 'en_progreso')
                """,
                (timestamp, started_colombia, len(items)),
            )
            run_id = cursor.lastrowid

        cursor.execute("SELECT id_product FROM products")
        known_product_ids = {str(row[0]).strip() for row in cursor.fetchall()}
        price_rows = []
        stock_rows = []
        for item in items:
            item_id = str(item.get("id") or "").strip()
            if not item_id or item_id not in known_product_ids:
                result["omitidos"] += 1
                continue

            iva = _iva_percentage(item)
            for price in item.get("price") or []:
                list_id = str(price.get("idPriceList") or "").strip()
                if list_id not in _PRICE_LIST_IDS or price.get("price") is None:
                    continue
                try:
                    final_price = price_with_iva(price["price"], iva)
                    price_rows.append(
                        (int(list_id), final_price, item_id, str(timestamp))
                    )
                except (TypeError, ValueError, ArithmeticError):
                    result["errores"] += 1

            for warehouse in (item.get("inventory") or {}).get("warehouses") or []:
                warehouse_id = str(warehouse.get("id") or "").strip()
                if warehouse_id not in _WAREHOUSES_FOR_CATALOG:
                    continue
                try:
                    available = Decimal(str(warehouse.get("availableQuantity") or 0))
                    remissioned = Decimal(
                        str(warehouse.get("remissionedQuantity") or 0)
                    )
                    stock = max(available - remissioned, Decimal("0"))
                    stock_rows.append(
                        (int(warehouse_id), int(item_id), int(stock), str(timestamp))
                    )
                except (TypeError, ValueError, ArithmeticError):
                    result["errores"] += 1

        if price_rows:
            cursor.executemany(
                """
                INSERT INTO precio_producto
                    (id_lista_precio, precio, id_producto, fecha_actualizacion)
                VALUES (%s, %s, %s, %s)
                """,
                price_rows,
            )
        if stock_rows:
            cursor.executemany(
                """
                INSERT INTO stock_bodegas
                    (id_bodega, id_producto, cantidad, fecha_actualizacion)
                VALUES (%s, %s, %s, %s)
                """,
                stock_rows,
            )
        result["precios"] = len(price_rows)
        result["stock"] = len(stock_rows)

        finished_timestamp, finished_colombia = execution_timestamp()
        if sync_runs_available and run_id is not None:
            cursor.execute(
                """
                UPDATE sync_runs
                SET timestamp_fin = %s, fecha_fin_colombia = %s,
                    total_precios = %s, total_stock = %s, total_errores = %s,
                    estado = %s, detalle = %s
                WHERE id_sync = %s
                """,
                (
                    finished_timestamp,
                    finished_colombia,
                    result["precios"],
                    result["stock"],
                    result["errores"],
                    "ok" if result["errores"] == 0 else "error_parcial",
                    f"timestamp={timestamp}",
                    run_id,
                ),
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
