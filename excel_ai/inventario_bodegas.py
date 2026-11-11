"""
Genera el Excel de inventario por bodega usando el endpoint de listado de Alegra.

Endpoint: GET https://api.alegra.com/api/v1/items
Params:   fields=remissionedQuantity  |  limit=30  |  type=product

Trae todos los items activos paginados de 30 en 30 (~65 páginas para 1921 productos).
Tiempo estimado: ~1-2 minutos en lugar de 13.

Columnas:
    TOTAL REAL | REFERENCIA | NOMBRE | INSTALADOR | CLIENTE | DCTO VOL | LINK
    Los precios de INSTALADOR y CLIENTE incluyen el IVA activo de cada producto.

  Stock   = availableQuantity
  Remis.  = remissionedQuantity (apartado en remisiones abiertas)
  Real    = Stock - Remis.

Colores:
  Remis. en rojo    → unidades comprometidas
  Real en naranja   → reducido por remisiones
  Real normal       → disponible al 100%
  Ceros en gris
  Precio en verde oscuro

Uso:
    python -m excel_ai.inventario_bodegas
    python -m excel_ai.inventario_bodegas --forzar --upload
    python -m excel_ai.inventario_bodegas --limpiar  # borrar checkpoint y salir
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv


# ── Rutas ───────────────────────────────────────────────────────────────────
PROJECT_ROOT    = Path(__file__).resolve().parent.parent
EXCEL_AI_DIR    = PROJECT_ROOT / "excel_ai"
OUTPUT_FILE     = EXCEL_AI_DIR / "inventario_bodegas.xlsx"
CHECKPOINT_FILE = EXCEL_AI_DIR / "checkpoint.json"
DATA_CACHE_FILE = EXCEL_AI_DIR / "data_cache.json"   # datos de la última consulta exitosa

load_dotenv(PROJECT_ROOT / ".env")
AUTH = HTTPBasicAuth(
    os.getenv("ALEGRA_EMAIL", ""),
    os.getenv("ALEGRA_API_TOKEN", ""),
)

# ── Config ──────────────────────────────────────────────────────────────────
BODEGAS    = ["Principal", "Caobos", "Bulevar"]
PAGE_SIZE  = 30    # máximo por página en Alegra
PAGE_DELAY = 0.3   # segundos entre páginas


# ════════════════════════════════════════════════════════════════════════════
# Checkpoint (por página para poder reanudar)
# ════════════════════════════════════════════════════════════════════════════

def _load_checkpoint() -> dict | None:
    if not CHECKPOINT_FILE.exists():
        return None
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            cp = json.load(f)
        if cp.get("date") == str(date.today()):
            return cp
        print("  ℹ Checkpoint de otro día — empezando desde 0")
        return None
    except Exception:
        return None


def _save_checkpoint(cp: dict):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)


def _clear_checkpoint():
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("  Checkpoint eliminado.")
    else:
        print("  No hay checkpoint que eliminar.")


# ════════════════════════════════════════════════════════════════════════════
# Fetch paginado
# ════════════════════════════════════════════════════════════════════════════

def _fetch_page(start: int, retries: int = 6) -> list | None:
    """Trae una página de items activos con reintentos ante fallos transitorios."""
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://api.alegra.com/api/v1/items",
                params={
                    "start":  start,
                    "limit":  PAGE_SIZE,
                    "type":   "product",
                    "fields": "remissionedQuantity,settingsOnShop",
                },
                auth=AUTH,
                timeout=(10, 60),
            )
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"\n  ⚠ Rate limit (429) — esperando {wait}s...", flush=True)
                time.sleep(wait)
            else:
                print(f"\n  ⚠ HTTP {r.status_code} en página start={start}")
                time.sleep(5)
        except requests.exceptions.Timeout as e:
            wait = min(5 * (2 ** attempt), 60)
            print(
                f"\n  ⚠ Timeout en start={start}: {e}. "
                f"Reintentando en {wait}s... ({attempt + 1}/{retries})",
                flush=True,
            )
            time.sleep(wait)
        except requests.exceptions.ConnectionError as e:
            wait = min(5 * (2 ** attempt), 60)
            print(
                f"\n  ⚠ Error de conexión en start={start}: {e}. "
                f"Reintentando en {wait}s... ({attempt + 1}/{retries})",
                flush=True,
            )
            time.sleep(wait)
    return None


def _is_inventory_product(item: dict) -> bool:
    """Filtra productos activos con referencia, incluyendo ocultos en tienda."""
    return (
        item.get("type") == "product"
        and item.get("status") == "active"
        and bool(str(item.get("reference") or "").strip())
    )


def fetch_all_inventory() -> list:
    """
    Descarga todos los items activos paginados de 30 en 30.
    Reanuda desde el último checkpoint si existe.
    """
    cp = _load_checkpoint()
    if cp is not None:
        items = cp.get("items", [])
        next_start = cp.get("next_start", 0)
        print(f"  ✓ Checkpoint: {len(items)} items ya descargados, retomando desde página {next_start // PAGE_SIZE + 1}...\n")
    else:
        items      = []
        next_start = 0
        cp = {"date": str(date.today()), "items": items, "next_start": 0}
        _save_checkpoint(cp)
        print(f"  Descargando inventario paginado (30 items/página)...")

    page = next_start // PAGE_SIZE + 1
    while True:
        batch = _fetch_page(next_start)

        if batch is None:
            raise RuntimeError(
                f"Descarga incompleta: falló la página {page}. "
                "No se generará ni subirá el Excel; reintenta la actualización."
            )

        # Este catálogo incluye activos con referencia aunque estén ocultos en tienda.
        activos = [
            i for i in batch
            if _is_inventory_product(i)
        ]
        items.extend(activos)

        fetched = len(batch)
        print(f"  Página {page}: {fetched} items ({len(items)} total)", flush=True)

        # Guardar checkpoint después de cada página
        next_start += PAGE_SIZE
        cp["items"]      = items
        cp["next_start"] = next_start
        _save_checkpoint(cp)

        if fetched < PAGE_SIZE:
            break

        page += 1
        time.sleep(PAGE_DELAY)

    print(f"\n  ✓ {len(items)} items activos descargados")

    # Guardar caché para regenerar Excel sin volver a la API
    with open(DATA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"date": str(date.today()), "items": items, "complete": True},
            f,
            ensure_ascii=False,
        )

    return items


# ════════════════════════════════════════════════════════════════════════════
# Parseo de bodegas y precios
# ════════════════════════════════════════════════════════════════════════════

def _parse_warehouses(item: dict) -> dict:
    """Retorna {bodega: {available, remissioned, real}}"""
    inv  = item.get("inventory", {}) or {}
    whs  = inv.get("warehouses", []) or []
    stock = {}
    for w in whs:
        name = w.get("name", "")
        if name in BODEGAS:
            av  = int(w.get("availableQuantity")   or 0)
            rem = int(w.get("remissionedQuantity") or 0)
            stock[name] = {"available": av, "remissioned": rem, "real": av - rem}
    return stock


def _parse_prices(item: dict) -> dict:
    """Retorna {instalador, cliente, alia2, dcto_volumen}"""
    prices = {"instalador": None, "cliente": None, "alia2": None}
    for p in (item.get("price", []) or []):
        name = (p.get("name") or "").upper()
        val  = p.get("price")
        if "INSTALADOR" in name:
            prices["instalador"] = val
        elif "CLIENTE" in name:
            prices["cliente"] = val
        elif "ALIA2" in name:
            prices["alia2"] = val

    dcto = False
    for cf in (item.get("customFields", []) or []):
        if cf.get("name") == "escala_descuentos" and str(cf.get("value") or "").strip():
            dcto = True
            break

    if not dcto and prices["alia2"] and prices["instalador"]:
        if prices["alia2"] < prices["instalador"]:
            dcto = True

    iva = 0.0
    for tax in item.get("tax") or []:
        if (
            isinstance(tax, dict)
            and str(tax.get("type", "")).upper() == "IVA"
            and str(tax.get("status", "")).lower() == "active"
        ):
            try:
                iva = float(tax.get("percentage") or 0)
            except (TypeError, ValueError):
                iva = 0.0
            break

    for key in ("instalador", "cliente"):
        if prices[key] is not None and iva > 0:
            try:
                prices[key] = round(float(prices[key]) * (1 + iva / 100))
            except (TypeError, ValueError):
                pass

    prices["dcto_volumen"] = dcto
    return prices


def _extract_link(item: dict) -> str:
    """Obtiene el campo personalizado LINK del producto de Alegra."""
    for field in item.get("customFields") or []:
        name = str(field.get("name") or field.get("label") or "").strip().lower()
        if name == "link":
            return str(field.get("value") or "").strip()
    return str(item.get("link") or item.get("url") or "").strip()


# ════════════════════════════════════════════════════════════════════════════
# Hoja CON STOCK
# ════════════════════════════════════════════════════════════════════════════

def _add_con_stock_sheet(wb, ws_inventario):
    """
    Crea la hoja 'CON STOCK' en la posición 0 del workbook.
    Copia las filas de encabezado (1-2) con sus merges y estilos desde
    ws_inventario,     luego copia solo las filas donde la columna A
    (TOTAL REAL) es un número > 0.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from copy import copy as _copy

    ws = wb.create_sheet("CON STOCK", 0)
    category_fill = PatternFill(fill_type="solid", fgColor="2E75B6")
    category_font = Font(bold=True, size=10, color="FFFFFF")

    def _copy_cell(src, dst):
        """Copia value y todos los atributos de estilo de src a dst."""
        dst.value = src.value
        if src.has_style:
            dst.font        = _copy(src.font)
            dst.fill        = _copy(src.fill)
            dst.alignment   = _copy(src.alignment)
            dst.border      = _copy(src.border)
            dst.number_format = src.number_format

    # ── Copiar filas 1 y 2 (encabezados) ────────────────────────────────────
    max_col = 7
    source_columns = (3, 1, 2, 13, 14, 15, 16)
    for r in (1, 2):
        for dst_col, src_col in enumerate(source_columns, 1):
            _copy_cell(ws_inventario.cell(row=r, column=src_col), ws.cell(row=r, column=dst_col))
        ws.row_dimensions[r].height = ws_inventario.row_dimensions[r].height

    # ── Copiar datos con stock, conservando los separadores de categoría ────
    dst_row = 3
    pending_category = None
    for src_row in range(3, ws_inventario.max_row + 1):
        category_value = ws_inventario.cell(row=src_row, column=1).value
        val_total = ws_inventario.cell(row=src_row, column=3).value
        if val_total is None and isinstance(category_value, str) and category_value.strip():
            pending_category = src_row
            continue
        if not isinstance(val_total, (int, float)) or val_total <= 0:
            continue

        if pending_category is not None:
            for dst_col, src_col in enumerate(source_columns, 1):
                _copy_cell(
                    ws_inventario.cell(row=pending_category, column=src_col),
                    ws.cell(row=dst_row, column=dst_col),
                )
            ws.cell(row=dst_row, column=1).value = ws_inventario.cell(
                row=pending_category, column=1
            ).value
            for category_col in range(1, max_col + 1):
                category_cell = ws.cell(row=dst_row, column=category_col)
                category_cell.fill = category_fill
                category_cell.font = category_font
                category_cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.merge_cells(
                start_row=dst_row,
                start_column=1,
                end_row=dst_row,
                end_column=max_col,
            )
            ws.row_dimensions[dst_row].height = ws_inventario.row_dimensions[pending_category].height
            dst_row += 1
            pending_category = None

        for dst_col, src_col in enumerate(source_columns, 1):
            _copy_cell(
                ws_inventario.cell(row=src_row, column=src_col),
                ws.cell(row=dst_row, column=dst_col),
            )
        ws.row_dimensions[dst_row].height = ws_inventario.row_dimensions[src_row].height
        dst_row += 1

    # ── Anchos de columna ────────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 46
    ws.column_dimensions["D"].width = 17
    ws.column_dimensions["E"].width = 17
    ws.column_dimensions["F"].width = 9
    ws.column_dimensions["G"].width = 55

    ws.freeze_panes = "B3"


# ════════════════════════════════════════════════════════════════════════════
# Generación del Excel
# ════════════════════════════════════════════════════════════════════════════

def _build_workbook(items_data: list):
    """Construye el workbook openpyxl con la hoja INVENTARIO y lo retorna sin guardarlo."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    # Agrupar por categoría
    cats: dict = {}
    for item in items_data:
        if not item.get("reference"):
            continue
        ic  = item.get("itemCategory", {}) or {}
        cat = (ic.get("name") or "OTROS").upper()
        if cat not in cats:
            cats[cat] = []
        cats[cat].append({
            "ref":    item.get("reference", ""),
            "nombre": item.get("name", ""),
            "stock":  _parse_warehouses(item),
            "prices": _parse_prices(item),
            "link":   _extract_link(item),
        })

    # ── Estilos ──────────────────────────────────────────────────────────────
    thin = Border(
        left=Side(style="thin", color="DDDDDD"),
        right=Side(style="thin", color="DDDDDD"),
        top=Side(style="thin", color="DDDDDD"),
        bottom=Side(style="thin", color="DDDDDD"),
    )
    hdr_font    = Font(bold=True, size=9,  color="FFFFFF")
    hdr_fill    = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    bodega_fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    no_fill     = PatternFill(fill_type=None)
    cat_font    = Font(bold=True, size=10, color="FFFFFF")
    cat_fill    = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    data_font   = Font(size=9)
    bold_font   = Font(size=9, bold=True)
    zero_font   = Font(size=9, color="BBBBBB")
    red_font    = Font(size=9, bold=True, color="C62828")
    orange_font = Font(size=9, bold=True, color="E65100")
    price_font  = Font(size=9, bold=True)
    # Nuevos estilos visuales
    ref_stock_fill  = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")  # verde suave
    ref_stock_font  = Font(size=9, bold=True, color="1B4332")                                    # texto verde oscuro
    zebra_fill      = PatternFill(start_color="F5F5F5", end_color="F5F5F5", fill_type="solid")  # gris tenue
    zebra_fill_ref  = PatternFill(start_color="EAF4E4", end_color="EAF4E4", fill_type="solid")  # verde+zebra
    stock_fill      = PatternFill(start_color="E2F0D9", end_color="E2F0D9", fill_type="solid")
    remission_fill  = PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid")
    real_fill       = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

    wb = Workbook()
    ws = wb.active
    ws.title = "INVENTARIO"

    # INVENTARIO conserva el formato normal con referencia y nombre primero.
    # A=REFERENCIA, B=NOMBRE, C=TOTAL REAL, D:L=stock por bodega,
    # M=INSTALADOR, N=CLIENTE, O=DCTO VOL, P=LINK.
    TOTAL_COLS = 16  # A..P

    # ── Fila 1: Headers principales ──────────────────────────────────────────
    headers_row1 = [
        "REFERENCIA", "NOMBRE", "TOTAL REAL",
        "STOCK", "REMIS.", "REAL",
        "STOCK", "REMIS.", "REAL",
        "STOCK", "REMIS.", "REAL",
        "INSTALADOR + IVA", "CLIENTE + IVA", "DCTO VOL", "LINK",
    ]
    for col, h in enumerate(headers_row1, 1):
        fill = hdr_fill
        cell           = ws.cell(row=1, column=col, value=h)
        cell.font      = hdr_font
        cell.fill      = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = thin
    ws.row_dimensions[1].height = 16

    # ── Fila 2: Sub-fila con nombres de bodegas debajo de sus columnas ────────
    for c in range(1, TOTAL_COLS + 1):
        cell = ws.cell(row=2, column=c)
        cell.fill   = hdr_fill
        cell.border = thin

    # Nombres de bodegas en la segunda fila.
    for start_col, label in [(4, "PRINCIPAL"), (7, "CAOBOS"), (10, "BULEVAR")]:
        ws.merge_cells(start_row=2, start_column=start_col, end_row=2, end_column=start_col + 2)
        cell = ws.cell(row=2, column=start_col, value=label)
        cell.font = Font(bold=True, size=9, color="FFFFFF")
        cell.fill = bodega_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin

    for c, label in [(13, "INSTALADOR + IVA"), (14, "CLIENTE + IVA"), (15, "DCTO VOL"), (16, "LINK")]:
        cell           = ws.cell(row=2, column=c, value=label)
        cell.font      = Font(bold=True, size=9, color="1F3864")
        cell.fill      = no_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = thin

    ws.row_dimensions[2].height = 14

    # ── Datos ─────────────────────────────────────────────────────────────────
    row = 3
    num = 1
    COP_FMT = '"$"#,##0'

    for cat in sorted(cats.keys()):
        # Banner categoría
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=TOTAL_COLS)
        cell           = ws.cell(row=row, column=1, value=cat)
        cell.font      = cat_font
        cell.fill      = cat_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border    = thin
        ws.row_dimensions[row].height = 15
        row += 1

        for idx, p in enumerate(sorted(cats[cat], key=lambda x: x["ref"])):
            # ── ¿tiene stock real en alguna bodega? ──────────────────────────
            tiene_stock = any(
                p["stock"].get(b, {}).get("real", 0) > 0
                for b in BODEGAS
            )
            # ── Zebra striping: filas pares en gris tenue ───────────────────
            es_zebra = (idx % 2 == 1)

            # Fill para cada celda de la fila (excepto col A que tiene su propio color)
            fila_fill = zebra_fill if es_zebra else no_fill

            total_real = sum(
                max(p["stock"].get(bodega, {}).get("real", 0), 0)
                for bodega in BODEGAS
            )
            # ── Columna C: TOTAL REAL ────────────────────────────────────────
            if tiene_stock:
                ref_fill = zebra_fill_ref if es_zebra else ref_stock_fill
                ref_font = ref_stock_font
            else:
                ref_fill = fila_fill
                ref_font = bold_font

            c_ref = ws.cell(row=row, column=3, value=total_real if total_real else "")
            c_ref.font      = ref_font
            c_ref.fill      = ref_fill
            c_ref.alignment = Alignment(horizontal="center")
            c_ref.border    = thin

            # ── Columna A: REFERENCIA ───────────────────────────────────────
            c_ref = ws.cell(row=row, column=1, value=p["ref"])
            c_ref.font = bold_font
            c_ref.fill = fila_fill
            c_ref.alignment = Alignment(horizontal="center")
            c_ref.border = thin

            # ── Columna B: NOMBRE ────────────────────────────────────────────
            c_nom = ws.cell(row=row, column=2, value=p["nombre"])
            c_nom.font      = data_font
            c_nom.fill      = fila_fill
            c_nom.border    = thin

            # ── Stock por bodega (cols D:L) ─────────────────────────────────
            stock_col = 4
            for bodega in BODEGAS:
                stock = p["stock"].get(bodega, {})
                for key in ("available", "remissioned", "real"):
                    value = stock.get(key, 0)
                    cell = ws.cell(row=row, column=stock_col, value=value if value else "")
                    cell.font = (
                        bold_font if key == "available" and value > 0
                        else red_font if key == "remissioned" and value > 0
                        else orange_font if key == "real" and value > 0 and stock.get("remissioned", 0) > 0
                        else bold_font if key == "real" and value > 0
                        else zero_font
                    )
                    cell.fill = (
                        remission_fill if key == "remissioned" and value > 0
                        else real_fill if key == "real" and value > 0 and stock.get("remissioned", 0) > 0
                        else stock_fill if key in {"available", "real"} and value > 0
                        else fila_fill
                    )
                    cell.alignment = Alignment(horizontal="center")
                    cell.border = thin
                    stock_col += 1

            # ── Precios (cols M=13, N=14) ────────────────────────────────────
            pr = p["prices"]
            for col_p, key in [(13, "instalador"), (14, "cliente")]:
                val  = pr.get(key)
                cell = ws.cell(row=row, column=col_p, value=val if val else "")
                cell.font      = price_font if val else zero_font
                cell.fill      = fila_fill
                cell.alignment = Alignment(horizontal="right")
                cell.border    = thin
                if val:
                    cell.number_format = COP_FMT

            # ── Col O: DCTO VOL ──────────────────────────────────────────────
            c_dcto = ws.cell(row=row, column=15, value="✓" if pr.get("dcto_volumen") else "")
            c_dcto.font      = Font(size=10, bold=True, color="1B5E20") if pr.get("dcto_volumen") else zero_font
            c_dcto.fill      = fila_fill
            c_dcto.alignment = Alignment(horizontal="center", vertical="center")
            c_dcto.border    = thin

            # ── Col P: LINK ─────────────────────────────────────────────────
            c_link = ws.cell(row=row, column=16, value=p["link"])
            c_link.font = data_font
            c_link.fill = fila_fill
            c_link.alignment = Alignment(horizontal="left")
            c_link.border = thin

            ws.row_dimensions[row].height = 13
            row += 1
            num += 1

    # ── Anchos de columna ─────────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 46
    for col in "DEFGHIJKL":
        ws.column_dimensions[col].width = 9
    ws.column_dimensions["M"].width = 17
    ws.column_dimensions["N"].width = 17
    ws.column_dimensions["O"].width = 9
    ws.column_dimensions["P"].width = 55

    ws.freeze_panes = "B3"
    ws.auto_filter.ref = f"A1:P{row - 1}"

    _add_con_stock_sheet(wb, ws)

    return wb, num - 1, len(cats)


def generate_excel(items_data: list):
    """Genera y guarda el Excel de inventario (sin hoja CAMBIOS).
    Usado por el CLI: python -m excel_ai.inventario_bodegas."""
    wb, num_productos, num_cats = _build_workbook(items_data)
    wb.save(str(OUTPUT_FILE))

    print(f"\n{'═' * 55}")
    print(f"  INVENTARIO GENERADO")
    print(f"{'═' * 55}")
    print(f"  Archivo:    {OUTPUT_FILE}")
    print(f"  Productos:  {num_productos}")
    print(f"  Categorías: {num_cats}")
    print(f"{'═' * 55}\n")


def _generate_excel_with_changes(items_data: list) -> None:
    """Genera el inventario y hornea la hoja oculta CAMBIOS con su historial."""
    wb, num_productos, num_cats = _build_workbook(items_data)
    from excel_ai.cambios_bodegas import aplicar_cambios

    aplicar_cambios(wb, items_data)
    wb.save(str(OUTPUT_FILE))

    print(f"\n{'═' * 55}")
    print("  INVENTARIO GENERADO")
    print(f"{'═' * 55}")
    print(f"  Archivo:    {OUTPUT_FILE}")
    print(f"  Productos:  {num_productos}")
    print(f"  Categorías: {num_cats}")
    print("  Hoja:       CAMBIOS (oculta)")
    print(f"{'═' * 55}\n")


# ════════════════════════════════════════════════════════════════════════════
# Función pública: genera Excel desde items.json (sin llamar a Alegra)
# ════════════════════════════════════════════════════════════════════════════

def generate_excel_from_json(items_path: str = "data/items.json"):
    """
    Genera el Excel de inventario leyendo directamente desde items.json.
    Usado por actualizar_catalogo.py paso 6 para evitar una segunda
    descarga de Alegra — los datos ya están frescos del paso 1.

    Filtra igual que fetch_all_inventory: solo productos activos con referencia,
    sin excluir los ocultos en tienda para este catálogo específico.
    """
    path = Path(items_path)
    if not path.exists():
        raise FileNotFoundError(f"No se encontró {items_path}. Ejecuta primero el paso 1.")

    with open(path, encoding="utf-8") as f:
        all_items = json.load(f)

    # Este catálogo conserva activos con referencia aunque estén ocultos en tienda.
    items_data = [
        i for i in all_items
        if _is_inventory_product(i)
    ]

    if len(items_data) < 500:
        raise RuntimeError(
            f"items.json tiene solo {len(items_data)} items activos con referencia — "
            "datos insuficientes para generar el inventario."
        )

    print(f"  Generando Excel desde {items_path} ({len(items_data)} items activos)...")
    wb, _, _ = _build_workbook(items_data)
    from excel_ai.cambios_bodegas import aplicar_cambios
    aplicar_cambios(wb, items_data)
    wb.save(str(OUTPUT_FILE))


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    if "--limpiar" in sys.argv:
        _clear_checkpoint()
        return

    upload_after_generation = "--upload" in sys.argv

    def _upload_if_requested():
        if upload_after_generation:
            from excel_ai.upload_drive import upload
            print("Subiendo inventario actualizado a Drive...")
            upload()

    # Usar caché del día si existe — evita volver a la API solo por cambios de formato
    if DATA_CACHE_FILE.exists() and "--forzar" not in sys.argv:
        try:
            with open(DATA_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == str(date.today()) and cache.get("complete") is True:
                items_data = cache["items"]
                print(f"Usando datos en caché del día ({len(items_data)} productos)...")
                print("Generando Excel...")
                _generate_excel_with_changes(items_data)
                _clear_checkpoint()
                _upload_if_requested()
                print("  ✓ Listo.")
                return
        except Exception:
            pass

    print("Consultando API de Alegra (paginado 30 en 30)...")
    items_data = fetch_all_inventory()

    print("Generando Excel...")
    _generate_excel_with_changes(items_data)

    _clear_checkpoint()
    _upload_if_requested()
    print("  ✓ Listo.")


if __name__ == "__main__":
    main()
