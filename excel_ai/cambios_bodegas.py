"""
Módulo de hoja oculta CAMBIOS para el inventario de bodegas.

Flujo (R1 — bake-before-upload atómico):
1. Antes de generar el xlsx: leer historial CAMBIOS del archivo en Drive (si existe)
   y calcular diffs contra el snapshot anterior.
2. Guardar el nuevo snapshot (atómicamente, ANTES de hornear CAMBIOS).
3. Hornear la hoja CAMBIOS (oculta, con todo el historial) dentro del workbook
   openpyxl — así la re-importación no puede perder el historial.
4. POST-subida: garantizar que la hoja CAMBIOS quede marcada como hidden en el
   Google Sheet nativo vía Sheets API batchUpdate.

Referencia de campos de stock:
    stock_principal, stock_caobos, stock_bulevar = Real (available - remissioned)
    stock_total = suma de los tres Reales
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from googleapiclient.errors import HttpError
from proyecto_ecxel.catalogo import is_reportable_product

# ── Rutas absolutas ──────────────────────────────────────────────────────────
PROJECT_ROOT        = Path(__file__).resolve().parent.parent
BODEGAS_SNAPSHOT_PATH = PROJECT_ROOT / "data" / "bodegas_snapshot.json"
DRIVE_ID_FILE       = Path(__file__).resolve().parent / "drive_id.json"

# ── Constantes ───────────────────────────────────────────────────────────────
BODEGAS       = ["Principal", "Caobos", "Bulevar"]
CAMPOS_STOCK  = ["stock_principal", "stock_caobos", "stock_bulevar", "stock_total"]
CAMBIOS_HEADERS = ["Fecha", "Referencia", "Nombre", "Campo Cambiado", "Valor Anterior", "Valor Nuevo"]
GOOGLE_SHEET_MIMETYPE = "application/vnd.google-apps.spreadsheet"


# ════════════════════════════════════════════════════════════════════════════
# Helper de reintentos (copia del patrón en proyecto_ecxel/catalogo_sheets.py)
# ════════════════════════════════════════════════════════════════════════════

def _execute_con_reintentos(request, intentos=5, espera_inicial=3):
    """Ejecuta una llamada a la API de Google reintentando ante errores
    temporales (500/503/429). Usa backoff: 3s, 6s, 12s, ..."""
    espera = espera_inicial
    for i in range(intentos):
        try:
            return request.execute()
        except HttpError as e:
            code = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
            if int(code or 0) in (429, 500, 503) and i < intentos - 1:
                print(f"  ⚠ Google respondió {code}, reintentando en {espera}s... ({i + 1}/{intentos})")
                time.sleep(espera)
                espera *= 2
            else:
                raise


# ════════════════════════════════════════════════════════════════════════════
# Stock por bodega
# ════════════════════════════════════════════════════════════════════════════

def _stock_por_bodega(item: dict) -> dict:
    """Retorna {stock_principal, stock_caobos, stock_bulevar, stock_total}
    usando Real = availableQuantity − remissionedQuantity por cada bodega en BODEGAS.
    stock_total = suma de los tres Reales.
    """
    inv  = item.get("inventory", {}) or {}
    whs  = inv.get("warehouses", []) or []

    reales = {}
    for w in whs:
        name = w.get("name", "")
        if name in BODEGAS:
            av  = int(w.get("availableQuantity")   or 0)
            rem = int(w.get("remissionedQuantity") or 0)
            reales[name] = av - rem

    principal = reales.get("Principal", 0)
    caobos    = reales.get("Caobos",    0)
    bulevar   = reales.get("Bulevar",   0)
    total     = principal + caobos + bulevar

    return {
        "stock_principal": principal,
        "stock_caobos":    caobos,
        "stock_bulevar":   bulevar,
        "stock_total":     total,
    }


# ════════════════════════════════════════════════════════════════════════════
# Snapshot
# ════════════════════════════════════════════════════════════════════════════

def _load_snapshot() -> dict:
    """Carga snapshot anterior de BODEGAS_SNAPSHOT_PATH.
    Retorna {} si no existe o está corrupto."""
    if not BODEGAS_SNAPSHOT_PATH.exists():
        return {}
    try:
        with open(BODEGAS_SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_snapshot(snapshot: dict) -> None:
    """Guarda snapshot en BODEGAS_SNAPSHOT_PATH con escritura atómica."""
    os.makedirs(str(BODEGAS_SNAPSHOT_PATH.parent), exist_ok=True)
    tmp = str(BODEGAS_SNAPSHOT_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(BODEGAS_SNAPSHOT_PATH))


def _construir_snapshot(items_data: list) -> dict:
    """Construye un snapshot {referencia: {nombre, stock_principal, stock_caobos,
    stock_bulevar, stock_total}} para items activos con referencia y visibles."""
    snapshot = {}
    for item in items_data:
        if item.get("status") != "active":
            continue
        if not is_reportable_product(item):
            continue
        ref    = item["reference"]
        nombre = item.get("name", "")
        stock  = _stock_por_bodega(item)
        snapshot[ref] = {
            "nombre":          nombre,
            "stock_principal": stock["stock_principal"],
            "stock_caobos":    stock["stock_caobos"],
            "stock_bulevar":   stock["stock_bulevar"],
            "stock_total":     stock["stock_total"],
        }
    return snapshot


# ════════════════════════════════════════════════════════════════════════════
# Cálculo de diferencias
# ════════════════════════════════════════════════════════════════════════════

def _compute_cambios(snapshot_anterior: dict, items_data: list) -> tuple:
    """Calcula diffs entre snapshot_anterior y el estado actual de items_data.

    Retorna (nuevas_filas, nuevo_snapshot).
    Cada fila: [Fecha, Referencia, Nombre, Campo Cambiado, Valor Anterior, Valor Nuevo]
    """
    ahora        = datetime.now().strftime("%d/%m/%Y %H:%M")
    nuevo_snapshot = _construir_snapshot(items_data)
    nuevas_filas = []

    # Caso especial: primera sincronización
    if not snapshot_anterior:
        nuevas_filas.append([ahora, "", "", "Primera sincronización", "", ""])
        return nuevas_filas, nuevo_snapshot

    # Diff: productos actuales contra snapshot anterior
    for ref, datos_nuevo in nuevo_snapshot.items():
        anterior = snapshot_anterior.get(ref)
        if anterior is None:
            # Producto nuevo
            nuevas_filas.append([
                ahora,
                ref,
                datos_nuevo.get("nombre", ""),
                "Producto nuevo",
                "",
                str(datos_nuevo.get("stock_total", "")),
            ])
            continue

        # Comparar campo por campo
        for campo in CAMPOS_STOCK:
            val_ant = anterior.get(campo)
            val_new = datos_nuevo.get(campo)
            if val_ant != val_new:
                nuevas_filas.append([
                    ahora,
                    ref,
                    datos_nuevo.get("nombre", ""),
                    campo,
                    str(val_ant) if val_ant is not None else "",
                    str(val_new) if val_new is not None else "",
                ])

    # Productos eliminados (en anterior pero ya no en el nuevo snapshot)
    for ref in snapshot_anterior:
        if ref not in nuevo_snapshot:
            datos_ant = snapshot_anterior[ref]
            nuevas_filas.append([
                ahora,
                ref,
                datos_ant.get("nombre", ""),
                "Producto eliminado",
                str(datos_ant.get("stock_total", "")),
                "",
            ])

    if nuevas_filas:
        nuevas_filas.insert(0, [ahora, "", "", "Actualización", "", ""])

    return nuevas_filas, nuevo_snapshot


# ════════════════════════════════════════════════════════════════════════════
# Lectura de historial existente en Drive
# ════════════════════════════════════════════════════════════════════════════

def _leer_existing_file_id() -> "str | None":
    """Lee drive_id.json y retorna el file_id o None."""
    if not DRIVE_ID_FILE.exists():
        return None
    try:
        with open(DRIVE_ID_FILE, encoding="utf-8") as f:
            return json.load(f).get("file_id")
    except Exception:
        return None


def _leer_cambios_existentes(file_id: "str | None") -> list:
    """Lee CAMBIOS del XLSX existente en Drive, preservando su historial."""
    if not file_id:
        return []

    from google.auth.transport.requests import AuthorizedSession
    from proyecto_ecxel.share_catalog import get_oauth_credentials

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    response = AuthorizedSession(get_oauth_credentials()).get(url, timeout=60)
    response.raise_for_status()
    return _leer_cambios_xlsx(response.content)


def _leer_cambios_xlsx(content: bytes) -> list:
    """Extrae las filas de CAMBIOS de un XLSX descargado de Drive."""
    from io import BytesIO
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        if "CAMBIOS" not in workbook.sheetnames:
            return []
        sheet = workbook["CAMBIOS"]
        return [
            [value if value is not None else "" for value in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    finally:
        workbook.close()


def _agrupar_historial(historial: list) -> list:
    """Inserta un separador por fecha en historiales antiguos sin agrupación."""
    if not historial:
        return []

    filas = [list(row) for row in historial if row and any(value not in (None, "") for value in row)]
    if not filas:
        return []

    if filas[0][:len(CAMBIOS_HEADERS)] != CAMBIOS_HEADERS:
        filas.insert(0, CAMBIOS_HEADERS)

    agrupado = [filas[0]]
    fecha_actual = None
    for fila in filas[1:]:
        fila += [""] * (len(CAMBIOS_HEADERS) - len(fila))
        fecha = fila[0]
        if fila[3] in {"Primera sincronización", "Actualización"}:
            fecha_actual = fecha
        elif fecha and fecha != fecha_actual:
            agrupado.append([fecha, "", "", "Actualización", "", ""])
            fecha_actual = fecha
        agrupado.append(fila)
    return agrupado


# ════════════════════════════════════════════════════════════════════════════
# Horneado de CAMBIOS en el workbook openpyxl (PRE-subida)
# ════════════════════════════════════════════════════════════════════════════

def _agregar_hoja_cambios_al_workbook(wb, filas: list) -> None:
    """Crea la hoja CAMBIOS (oculta) en el workbook con el historial completo.
    filas debe incluir el encabezado como primera fila cuando corresponda.
    """
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    ws = wb.create_sheet("CAMBIOS")
    thin = Side(style="thin", color="D9EAD3")
    header_fill = PatternFill(fill_type="solid", fgColor="1F3864")
    header_font = Font(bold=True, color="FFFFFF", size=9)
    separator_fill = PatternFill(fill_type="solid", fgColor="D1E3F4")
    normal_fill = PatternFill(fill_type="solid", fgColor="E2F0D9")
    warning_fill = PatternFill(fill_type="solid", fgColor="F4CCCC")

    for row_index, fila in enumerate(filas, 1):
        normalized = list(fila[:len(CAMBIOS_HEADERS)])
        normalized += [""] * (len(CAMBIOS_HEADERS) - len(normalized))
        ws.append(normalized)
        for cell in ws[row_index]:
            cell.border = Border(bottom=thin)
            cell.alignment = Alignment(vertical="center")

        if row_index == 1:
            for cell in ws[row_index]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.row_dimensions[row_index].height = 24
        elif normalized[3] in {"Primera sincronización", "Actualización"}:
            for cell in ws[row_index]:
                cell.fill = separator_fill
                cell.font = Font(bold=True)
            ws.row_dimensions[row_index].height = 20
        else:
            fill = _color_para_cambio(normalized, normal_fill, warning_fill)
            for cell in ws[row_index]:
                cell.fill = fill

    widths = [19, 20, 46, 28, 18, 18]
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + index)].width = width
    ws.freeze_panes = "A2"
    if ws.max_row >= 1:
        ws.auto_filter.ref = f"A1:F{ws.max_row}"
    ws.sheet_state = "hidden"


def _es_disminucion(valor_anterior, valor_nuevo) -> bool:
    """Identifica cambios numéricos descendentes."""
    try:
        return float(valor_nuevo) < float(valor_anterior)
    except (TypeError, ValueError):
        return False


def _color_para_cambio(fila: list, disminucion_fill, aumento_fill):
    """Selecciona color semántico para una fila de historial."""
    tipo = str(fila[3]).strip() if len(fila) > 3 else ""
    if tipo == "Producto eliminado" or _es_disminucion(
        fila[4] if len(fila) > 4 else "",
        fila[5] if len(fila) > 5 else "",
    ):
        return disminucion_fill
    return aumento_fill


def aplicar_cambios(wb, items_data: list) -> None:
    """Flujo PRE-subida: lee historial existente, calcula diffs, guarda snapshot
    y hornea la hoja CAMBIOS (oculta) en el workbook openpyxl.

    Orden de atomicidad:
    1. Leer historial previo de Drive.
    2. Calcular nuevas filas y nuevo snapshot.
    3. Guardar snapshot en disco (ANTES de hornear CAMBIOS).
    4. Hornear hoja CAMBIOS en el workbook.
    """
    existing_id    = _leer_existing_file_id()
    historial_previo = _agrupar_historial(_leer_cambios_existentes(existing_id))
    snapshot_anterior = _load_snapshot()

    nuevas_filas, nuevo_snapshot = _compute_cambios(snapshot_anterior, items_data)

    # Guardar snapshot ANTES de hornear CAMBIOS (atomicidad)
    _save_snapshot(nuevo_snapshot)

    if historial_previo:
        # historial_previo ya incluye encabezado (o no) desde Drive
        combined = historial_previo + nuevas_filas
    else:
        combined = [CAMBIOS_HEADERS] + nuevas_filas

    _agregar_hoja_cambios_al_workbook(wb, combined)

    n = len(nuevas_filas)
    if n:
        print(f"  📋 CAMBIOS: {n} fila(s) nueva(s) horneada(s) en el workbook.")
    else:
        print("  ℹ️ CAMBIOS: sin diferencias respecto al snapshot anterior.")
