"""
catalog_cache.py — Caché persistente del catálogo en data/

Genera archivos que la app web lee directamente (sin queries MySQL ni
renderizado Jinja2 en cada visita). Se regeneran al final de sync_db.py.

Archivos generados:
  data/catalog_cache_instalador.json   — precios INSTALADOR (JSON)
  data/catalog_cache_cliente.json      — precios CLIENTE    (JSON)
  data/catalog_html_cliente.html       — HTML pre-renderizado del catálogo cliente

Uso desde sync_db.py:
  from src.catalog_cache import rebuild_all
  rebuild_all()

Uso desde app.py:
  from src.catalog_cache import read_instalador, read_cliente, read_html_cliente
  data = read_instalador()
  data = read_cliente()
  html = read_html_cliente()   # str con el HTML de filas/tarjetas listo para inyectar
"""

import json
import os
from datetime import date
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

# ── Paths ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

CACHE_INSTALADOR = _ROOT / "data" / "catalog_cache_instalador.json"
CACHE_CLIENTE    = _ROOT / "data" / "catalog_cache_cliente.json"
CACHE_HTML       = _ROOT / "data" / "catalog_html_cliente.html"

# ── Conexión MySQL (lee del .env igual que app.py) ───────────────────────────
def _db_conn():
    return mysql.connector.connect(
        host     = os.getenv("MYSQL_HOST",     "127.0.0.1"),
        port     = int(os.getenv("MYSQL_PORT", "3306")),
        user     = os.getenv("MYSQL_USER",     "root"),
        password = os.getenv("MYSQL_PASSWORD", ""),
        database = os.getenv("MYSQL_DATABASE", "inventario_local"),
        charset  = "utf8mb4",
    )


# ── Generadores ──────────────────────────────────────────────────────────────

def build_instalador() -> dict:
    """Genera el catálogo INSTALADOR desde MySQL y lo guarda en JSON."""
    conn   = _db_conn()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            p.id_product,
            p.nombre,
            p.ref_producto       AS referencia,
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
        ORDER BY p.categoria, LEFT(p.nombre, 1),
                 pi.precio
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    productos = []
    for row in rows:
        precio_base    = float(row["precio_instalador"] or 0)
        pct_iva        = float(row["porcentaje_iva"] or 0)
        precio_con_iva = round(precio_base * (1 + pct_iva / 100)) if precio_base > 0 else 0
        precio_aliados = float(row["precio_aliados"] or 0)
        dcto_volumen   = bool(
            precio_aliados > 0 and precio_aliados < precio_base
        )
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
            "dcto_volumen":   dcto_volumen,
            "imagen_url":     row["ruta_imagen"] or "",
        })

    data = _agrupar(productos)
    _guardar(CACHE_INSTALADOR, data)
    print(f"[catalog_cache] instalador: {len(productos)} productos → {CACHE_INSTALADOR.name}")
    return data


def build_cliente() -> dict:
    """Genera el catálogo CLIENTE desde MySQL y lo guarda en JSON."""
    conn   = _db_conn()
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

    data = _agrupar(productos)
    _guardar(CACHE_CLIENTE, data)
    print(f"[catalog_cache] cliente:    {len(productos)} productos → {CACHE_CLIENTE.name}")
    return data


def build_html_cliente(data: dict | None = None) -> str:
    """
    Pre-renderiza el HTML de filas (tabla desktop) del catálogo cliente.
    El resultado se guarda en data/catalog_html_cliente.html y app.py lo
    inyecta directamente, evitando el loop Jinja2 sobre 2000+ productos.
    """
    from html import escape

    if data is None:
        data = read_cliente()
    if data is None:
        data = build_cliente()

    parts = []
    for cat, items in data["productos_por_categoria"].items():
        cat_esc = escape(cat)
        parts.append(
            f'<tr class="category-row" data-category="{cat_esc}">'
            f'<td colspan="6">{cat_esc}</td></tr>\n'
        )
        for item in items:
            nombre   = escape(item["nombre"])
            ref      = escape(item["referencia"])
            precio_f = escape(item["precio_formato"])
            precio_n = item["precio_con_iva"]
            stock    = item["stock"]
            img      = escape(item["imagen_url"])
            stock_cls = "available" if stock > 0 else "unavailable"
            stock_txt = "✔ Disponible" if stock > 0 else "Consultar"
            price_cls = "contact" if precio_n <= 10 else ""
            img_td    = f'<img src="{img}" alt="{nombre}" loading="lazy">' if img else ""

            parts.append(
                f'<tr class="product-row"'
                f' data-category="{cat_esc}"'
                f' data-name="{nombre.upper()}"'
                f' data-code="{ref.upper()}"'
                f' data-price="{precio_n}"'
                f' data-nombre="{nombre}"'
                f' data-ref="{ref}"'
                f' data-precio="{precio_f}"'
                f' data-img="{img}"'
                f' data-stock="{stock}">\n'
                f'  <td class="col-cotizar"><input type="checkbox" class="chk-cotizar"'
                f' onchange="toggleCotizar(this)" aria-label="Cotizar {nombre}"></td>\n'
                f'  <td class="col-code">{ref}</td>\n'
                f'  <td class="col-img">{img_td}</td>\n'
                f'  <td class="col-name">{nombre}</td>\n'
                f'  <td class="col-price {price_cls}">{precio_f}</td>\n'
                f'  <td class="col-stock {stock_cls}">{stock_txt}</td>\n'
                f'</tr>\n'
            )

    html = "".join(parts)
    CACHE_HTML.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_HTML.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(CACHE_HTML)
    total = sum(len(v) for v in data["productos_por_categoria"].values())
    print(f"[catalog_cache] html:       {total} productos → {CACHE_HTML.name}")
    return html


def read_html_cliente() -> str | None:
    """Lee el HTML pre-renderizado del catálogo cliente. None si no existe."""
    try:
        return CACHE_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def rebuild_all() -> None:
    print("\n[catalog_cache] Regenerando cachés de catálogo...")
    try:
        build_instalador()
        data = build_cliente()
        build_html_cliente(data)
        print("[catalog_cache] ✓ Cachés actualizados.")
    except Exception as e:
        print(f"[catalog_cache] ✗ Error al regenerar caché: {e}")
        raise


# ── Lectores (usados por app.py) ─────────────────────────────────────────────

def read_instalador() -> dict | None:
    """Lee el caché INSTALADOR. Devuelve None si no existe."""
    return _leer(CACHE_INSTALADOR)


def read_cliente() -> dict | None:
    """Lee el caché CLIENTE. Devuelve None si no existe."""
    return _leer(CACHE_CLIENTE)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _agrupar(productos: list) -> dict:
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


def _guardar(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(path)   # escritura atómica


def _leer(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
