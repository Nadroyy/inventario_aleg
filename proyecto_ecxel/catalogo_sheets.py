"""
Genera catálogo maestro como Google Spreadsheet nativo.
Mucho más rápido que subir un .xlsx — las copias son instantáneas.

Columnas: #, CÓDIGO, IMAGEN, NOMBRE, VALOR IVA INCLUIDO, DISPONIBLE
Precio: INSTALADOR + IVA 19%
Stock: Unificado (✔ Disponible / ✗ Agotado)
Precios < 10: Muestra "⚠️ Cotizar"
"""

import json
import os
import time
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from googleapiclient.errors import HttpError


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
                print(f"  ⚠ Google respondió {code}, reintentando en {espera}s... ({i+1}/{intentos})")
                time.sleep(espera)
                espera *= 2
            else:
                raise

def _load_snapshot(path: str) -> dict:
    """Carga snapshot anterior. Retorna {} si no existe o está corrupto."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_snapshot(path: str, activos: list, inactivos: list) -> None:
    """Guarda snapshot del estado actual de todos los productos visibles."""
    snapshot = {}
    for d in activos:
        snapshot[d["id"]] = {
            "nombre":     d["nombre"],
            "precio":     d["precio_con_iva"],
            "stock":      d["stock"],
            "estado":     "active",
            "categoria":  d["categoria"],
            "referencia": d.get("referencia", ""),
        }
    for d in inactivos:
        snapshot[d["id"]] = {
            "nombre":     d["nombre"],
            "precio":     d["precio_con_iva"],
            "stock":      d["stock"],
            "estado":     "inactive",
            "categoria":  d["categoria"],
            "referencia": d.get("referencia", ""),
        }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


# Reusar la lógica de extracción de datos
from proyecto_ecxel.catalogo import load_items, extract_catalog_data, is_reportable_product


def _update_changes_sheet(
    sheets_service,
    spreadsheet_id: str,
    sheet_id_cambios: int,
    snapshot_anterior: dict,
    activos: list,
    inactivos: list,
) -> None:
    """
    Compara snapshot_anterior contra el estado actual y agrega filas de cambios
    al final de la hoja CAMBIOS. Nunca borra el historial existente.

    Columnas: Fecha | ID Producto | Referencia | Nombre | Campo Cambiado | Valor Anterior | Valor Nuevo
    """
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
    CAMPOS = ["nombre", "precio", "stock", "estado", "categoria"]

    # Construir snapshot nuevo
    nuevo = {}
    for d in activos:
        nuevo[d["id"]] = {
            "nombre":     d["nombre"],
            "precio":     d["precio_con_iva"],
            "stock":      d["stock"],
            "estado":     "active",
            "categoria":  d["categoria"],
            "referencia": d.get("referencia", ""),
        }
    for d in inactivos:
        nuevo[d["id"]] = {
            "nombre":     d["nombre"],
            "precio":     d["precio_con_iva"],
            "stock":      d["stock"],
            "estado":     "inactive",
            "categoria":  d["categoria"],
            "referencia": d.get("referencia", ""),
        }

    nuevas_filas = []

    # Caso especial: primera sincronización (no hay snapshot previo)
    if not snapshot_anterior:
        nuevas_filas.append([ahora, "", "", "Primera sincronización", "", "", ""])
    else:
        # Diff: comparar cada producto nuevo contra el anterior
        for item_id, datos_nuevo in nuevo.items():
            anterior = snapshot_anterior.get(item_id)
            if anterior is None:
                # Producto nuevo — registrar como tal
                nuevas_filas.append([
                    ahora,
                    item_id,
                    datos_nuevo.get("referencia", ""),
                    datos_nuevo.get("nombre", ""),
                    "Producto nuevo",
                    "",
                    datos_nuevo.get("estado", ""),
                ])
                continue
            # Comparar campo por campo
            for campo in CAMPOS:
                val_ant = anterior.get(campo)
                val_new = datos_nuevo.get(campo)
                if val_ant != val_new:
                    nuevas_filas.append([
                        ahora,
                        item_id,
                        datos_nuevo.get("referencia", ""),
                        datos_nuevo.get("nombre", ""),
                        campo,
                        str(val_ant) if val_ant is not None else "",
                        str(val_new) if val_new is not None else "",
                    ])

        # Productos eliminados (en anterior pero ya no en nuevo)
        for item_id in snapshot_anterior:
            if item_id not in nuevo:
                datos_ant = snapshot_anterior[item_id]
                nuevas_filas.append([
                    ahora,
                    item_id,
                    datos_ant.get("referencia", ""),
                    datos_ant.get("nombre", ""),
                    "Producto eliminado del catálogo",
                    datos_ant.get("estado", ""),
                    "",
                ])

    if snapshot_anterior and nuevas_filas:
        nuevas_filas.insert(0, [ahora, "", "", "Actualización", "", "", ""])

    if not nuevas_filas:
        print("  ℹ️ Sin cambios detectados respecto al snapshot anterior.")
        return

    # Encontrar la última fila con datos para agregar al final
    resultado = _execute_con_reintentos(
        sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="CAMBIOS!A:A",
        )
    )
    valores_existentes = resultado.get("values", [])

    # Fix 1: filtrar filas vacías antes de contar (la API puede devolver [""] o [])
    filas_reales = [r for r in valores_existentes if r and r[0] and r[0] != ""]

    # Si la hoja está vacía (sin filas con contenido real), agregar encabezado primero
    if not any(r and r[0] for r in valores_existentes):
        header = [["Fecha", "ID Producto", "Referencia", "Nombre", "Campo Cambiado", "Valor Anterior", "Valor Nuevo"]]
        _execute_con_reintentos(
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="CAMBIOS!A1",
                valueInputOption="RAW",
                body={"values": header},
            )
        )
        next_row = 2
    else:
        next_row = len(filas_reales) + 1

    # El historial crece indefinidamente; ampliar la cuadrícula antes de
    # escribir para evitar que la API rechace la fila cuando supera el tamaño
    # inicial de 1000 filas.
    required_rows = next_row + len(nuevas_filas) - 1
    _execute_con_reintentos(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id_cambios,
                        "gridProperties": {"rowCount": required_rows + 100},
                    },
                    "fields": "gridProperties.rowCount",
                }
            }]},
        )
    )

    # Agregar filas al final
    _execute_con_reintentos(
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"CAMBIOS!A{next_row}",
            valueInputOption="RAW",
            body={"values": nuevas_filas},
        )
    )

    # Fix 2+3: aplicar colores (zebra + color de cambio) en un único batchUpdate
    def _color_for_row(fila: list) -> dict | None:
        """Devuelve el color de fondo RGB para una fila de cambios, o None si no aplica."""
        etiqueta = str(fila[3]) if len(fila) > 3 else ""
        campo   = str(fila[4]) if len(fila) > 4 else ""
        val_ant = str(fila[5]) if len(fila) > 5 else ""
        val_new = str(fila[6]) if len(fila) > 6 else ""

        if etiqueta in {"Primera sincronización", "Actualización"}:
            return {"red": 0.82, "green": 0.89, "blue": 0.96}
        if campo == "stock":
            try:
                if int(val_new) > int(val_ant):
                    return {"red": 1.0, "green": 0.6, "blue": 0.6}       # ROJO: stock sube
                elif int(val_new) < int(val_ant):
                    return {"red": 0.714, "green": 0.882, "blue": 0.804}  # VERDE: stock baja
            except (ValueError, TypeError):
                pass
        elif "Producto nuevo" in campo or "Producto eliminado" in campo:
            return {"red": 1.0, "green": 0.898, "blue": 0.6}             # AMARILLO
        return None

    fmt_requests = []
    GRIS = {"red": 0.949, "green": 0.949, "blue": 0.949}

    # Fix 3: zebra — filas pares de la hoja en gris claro (se agregan PRIMERO)
    for i, _ in enumerate(nuevas_filas):
        fila_abs = next_row + i  # posición 1-based en la hoja
        if fila_abs % 2 == 0:   # par → gris
            fmt_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id_cambios,
                        "startRowIndex": next_row - 1 + i,
                        "endRowIndex":   next_row + i,
                        "startColumnIndex": 0,
                        "endColumnIndex":   7,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": GRIS}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    # Fix 2: color de cambio — sobreescribe zebra donde aplica (se agregan DESPUÉS)
    for i, fila in enumerate(nuevas_filas):
        color = _color_for_row(fila)
        if color is not None:
            es_separador = str(fila[3]) in {"Primera sincronización", "Actualización"}
            fmt_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id_cambios,
                        "startRowIndex": next_row - 1 + i,
                        "endRowIndex":   next_row + i,
                        "startColumnIndex": 0,
                        "endColumnIndex":   7,
                    },
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": color,
                        "textFormat": {"bold": es_separador},
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
                }
            })

    if fmt_requests:
        _execute_con_reintentos(
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": fmt_requests},
            )
        )

    print(f"  📋 CAMBIOS: {len(nuevas_filas)} fila(s) registrada(s) en la hoja CAMBIOS.")


def _catalog_item_sort_key(item: dict) -> tuple[str, float]:
    name = str(item.get("nombre") or "")
    return (name[:1].upper(), float(item.get("precio_con_iva") or 0))


def _reset_spreadsheet_sheets(sheets_service, spreadsheet_id, props_catalogo, props_inactivos):
    """Deja un spreadsheet EXISTENTE con solo dos hojas limpias: CATÁLOGO e INACTIVOS.

    Permite reutilizar el mismo archivo (mismo ID/URL) en cada actualización, en
    vez de borrar+crear. Estrategia: crear una hoja temporal, borrar todas las
    demás (salvo CAMBIOS si existe), recrear CATÁLOGO e INACTIVOS, y borrar la
    temporal. Sheets exige que siempre exista al menos una hoja, de ahí la temporal.
    
    Retorna: (sid_cat, sid_inact, sid_cambios)
    """
    meta = _execute_con_reintentos(
        sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)"
        )
    )
    old_sheets = meta.get("sheets", [])

    # Detectar si ya existe la hoja CAMBIOS para preservarla
    cambios_sheet = next(
        (s for s in old_sheets if s["properties"]["title"] == "CAMBIOS"), None
    )
    cambios_sid_existente = cambios_sheet["properties"]["sheetId"] if cambios_sheet else None

    # 1. Crear hoja temporal para no quedarnos sin hojas al borrar
    add_tmp = _execute_con_reintentos(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "__TMP__"}}}]},
        )
    )
    tmp_id = add_tmp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # 2. Borrar todas las hojas viejas EXCEPTO CAMBIOS (para preservar el historial)
    del_reqs = [
        {"deleteSheet": {"sheetId": s["properties"]["sheetId"]}}
        for s in old_sheets
        if s["properties"]["title"] != "CAMBIOS"
    ]
    if del_reqs:
        _execute_con_reintentos(
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": del_reqs}
            )
        )

    # 3. Crear CATÁLOGO (index 0) e INACTIVOS, fijar locale
    #    Solo crear CAMBIOS si no existía previamente
    add_reqs = [
        {"updateSpreadsheetProperties": {"properties": {"locale": "es_CO"}, "fields": "locale"}},
        {"addSheet": {"properties": dict(props_catalogo, index=0)}},
        {"addSheet": {"properties": props_inactivos}},
    ]
    if cambios_sid_existente is None:
        add_reqs.append({"addSheet": {"properties": {
            "title": "CAMBIOS",
            "hidden": True,
            "gridProperties": {"rowCount": 1000, "columnCount": 7},
        }}})

    res = _execute_con_reintentos(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": add_reqs}
        )
    )
    # Los replies de addSheet vienen en orden
    nuevos = [r["addSheet"]["properties"] for r in res["replies"] if "addSheet" in r]
    sid_cat = next(p["sheetId"] for p in nuevos if p["title"] == "CATÁLOGO")
    sid_inact = next(p["sheetId"] for p in nuevos if p["title"] == "INACTIVOS")

    # Obtener sid_cambios: usar el existente o el recién creado
    sid_cambios = cambios_sid_existente
    if sid_cambios is None:
        sid_cambios = next(
            (p["sheetId"] for p in nuevos if p["title"] == "CAMBIOS"), None
        )

    # 4. Borrar la hoja temporal
    _execute_con_reintentos(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": tmp_id}}]},
        )
    )
    return sid_cat, sid_inact, sid_cambios


def generate_master_spreadsheet(sheets_service, items_file="data/items.json", solo_con_stock=False,
                                 drive_service=None, folder_id=None, existing_id=None):
    """
    Genera el catálogo maestro como Google Spreadsheet.
    Retorna el file_id del spreadsheet.

    - existing_id: si se pasa y el archivo existe, se REUTILIZA (mismo ID/URL),
      limpiando y reescribiendo su contenido. Evita duplicados y da una URL estable.
    - drive_service + folder_id: si no hay existing_id, crea el archivo DENTRO de
      esa carpeta vía Drive (necesario con service account). Si no, sheets.create().
    """
    items = load_items(items_file)

    # Separar activos e inactivos con los mismos filtros base del inventario.
    activos   = [item for item in items if is_reportable_product(item) and item.get("status") == "active"]
    inactivos = [item for item in items if is_reportable_product(item) and item.get("status") != "active"]

    datos = [extract_catalog_data(item) for item in activos]
    datos_inactivos = [extract_catalog_data(item) for item in inactivos]

    if solo_con_stock:
        datos = [d for d in datos if d["stock"] > 0]

    # ─── Snapshot: cargar estado previo para detectar cambios ───
    _SNAPSHOT_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "catalog_snapshot.json"
    )
    snapshot_anterior = _load_snapshot(_SNAPSHOT_PATH)

    # Agrupar activos por categoría
    categorias = {}
    for d in datos:
        cat = d["categoria"]
        if cat not in categorias:
            categorias[cat] = []
        categorias[cat].append(d)
    categorias = dict(sorted(categorias.items()))

    # Agrupar inactivos por categoría
    categorias_inact = {}
    for d in datos_inactivos:
        cat = d["categoria"]
        if cat not in categorias_inact:
            categorias_inact[cat] = []
        categorias_inact[cat].append(d)
    categorias_inact = dict(sorted(categorias_inact.items()))

    def _build_rows(cats_dict):
        """Construye las filas de datos para una agrupación de categorías."""
        rows = []
        rows.append(["", "LISTA DE PRECIOS - IVA INCLUIDO. SUJETA A CAMBIOS POR DISPONIBILIDAD", "", "", "", "", ""])
        rows.append(["", "La información proporcionada no es una propuesta comercial firme", "", "", "", "", ""])
        rows.append(["", f"Actualizado: {date.today().strftime('%d/%m/%Y')}", "", "", "", "", ""])
        rows.append(["", "", "", "", "", "", ""])
        rows.append(["#", "CÓDIGO", "IMAGEN", "NOMBRE", "DISPONIBLE", "DCTO VOL", "VALOR IVA INCLUIDO"])

        item_num = 1
        # Guardar índices de filas de categoría para formateo posterior
        cat_row_indices = []
        for categoria, items_cat in cats_dict.items():
            cat_row_indices.append(len(rows))  # índice 0-based de esta fila
            rows.append(["__CAT__", categoria, "", "", "", "", ""])
            for d in sorted(items_cat, key=_catalog_item_sort_key):
                precio_str = f"${d['precio_con_iva']:,.0f}" if d["precio_con_iva"] > 10 else "⚠️ Cotizar"
                disponible = "✔ Disponible" if d["stock"] > 0 else "⚠️ Cotizar"
                dcto = "✓" if d.get("dcto_volumen") else ""
                rows.append([item_num, d["referencia"], "", d["nombre"], disponible, dcto, precio_str])
                item_num += 1
        return rows, item_num - 1, cat_row_indices

    rows_activos, total_activos, cat_rows_activos = _build_rows(categorias)
    rows_inactivos, total_inactivos, cat_rows_inactivos = _build_rows(categorias_inact)

    props_catalogo = {
        "title": "CATÁLOGO",
        "gridProperties": {"rowCount": len(rows_activos) + 10, "columnCount": 7, "frozenRowCount": 5},
    }
    props_inactivos = {
        "title": "INACTIVOS",
        "hidden": True,
        "gridProperties": {"rowCount": max(len(rows_inactivos) + 10, 50), "columnCount": 7, "frozenRowCount": 5},
    }

    if existing_id:
        # ─── Reutilizar el maestro EXISTENTE (mismo ID/URL) ───
        # Limpia sus hojas y las recrea; el archivo y su link no cambian.
        spreadsheet_id = existing_id
        sheet_id_catalogo, sheet_id_inactivos, sheet_id_cambios = _reset_spreadsheet_sheets(
            sheets_service, spreadsheet_id, props_catalogo, props_inactivos
        )
    elif drive_service is not None and folder_id:
        # ─── Vía Drive: crear el archivo DENTRO de la carpeta compartida ───
        # (service account no tiene Drive propio; sheets.create() daría 403).
        # El archivo nace con una hoja por defecto; luego la adaptamos.
        created = _execute_con_reintentos(
            drive_service.files().create(
                body={
                    "name": "MAESTRO_CATALOGO",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "parents": [folder_id],
                },
                fields="id",
            )
        )
        spreadsheet_id = created["id"]

        # Leer la hoja por defecto y reconfigurar: renombrar a CATÁLOGO + crear INACTIVOS
        meta = _execute_con_reintentos(
            sheets_service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties(sheetId,title,index)"
            )
        )
        default_sheet_id = meta["sheets"][0]["properties"]["sheetId"]

        _execute_con_reintentos(
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [
                    {"updateSpreadsheetProperties": {
                        "properties": {"locale": "es_CO"}, "fields": "locale"}},
                    {"updateSheetProperties": {
                        "properties": {
                            "sheetId": default_sheet_id,
                            "title": "CATÁLOGO",
                            "gridProperties": props_catalogo["gridProperties"],
                        },
                        "fields": "title,gridProperties(rowCount,columnCount,frozenRowCount)",
                    }},
                    {"addSheet": {"properties": props_inactivos}},
                    {"addSheet": {"properties": {
                        "title": "CAMBIOS",
                        "hidden": True,
                        "gridProperties": {"rowCount": 1000, "columnCount": 7},
                    }}},
                ]}
            )
        )

        # Releer para conocer los sheetId definitivos
        meta = _execute_con_reintentos(
            sheets_service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties(sheetId,title)"
            )
        )
        sheet_id_catalogo = default_sheet_id
        sheet_id_inactivos = next(
            s["properties"]["sheetId"] for s in meta["sheets"]
            if s["properties"]["title"] == "INACTIVOS"
        )
        sheet_id_cambios = next(
            s["properties"]["sheetId"] for s in meta["sheets"]
            if s["properties"]["title"] == "CAMBIOS"
        )
    else:
        # ─── Vía Sheets: modo OAuth de usuario (crea en el Drive del usuario) ───
        spreadsheet_body = {
            "properties": {"title": "MAESTRO_CATALOGO", "locale": "es_CO"},
            "sheets": [
                {"properties": props_catalogo},
                {"properties": props_inactivos},
                {"properties": {
                    "title": "CAMBIOS",
                    "hidden": True,
                    "gridProperties": {"rowCount": 1000, "columnCount": 7},
                }},
            ],
        }
        spreadsheet = _execute_con_reintentos(
            sheets_service.spreadsheets().create(
                body=spreadsheet_body,
                fields="spreadsheetId,sheets.properties.sheetId"
            )
        )
        spreadsheet_id = spreadsheet["spreadsheetId"]
        sheet_id_catalogo  = spreadsheet["sheets"][0]["properties"]["sheetId"]
        sheet_id_inactivos = spreadsheet["sheets"][1]["properties"]["sheetId"]
        sheet_id_cambios   = spreadsheet["sheets"][2]["properties"]["sheetId"]

    # Escribir datos CATÁLOGO (limpiar tag __CAT__ antes de subir)
    def _clean_rows(rows):
        cleaned = []
        for r in rows:
            if r and r[0] == "__CAT__":
                # 7 columnas: A vacío, B=nombre categoría, C..G vacíos
                cleaned.append(["", r[1], r[2], r[3], r[4], r[5], r[6]])
            else:
                cleaned.append(r)
        return cleaned

    _execute_con_reintentos(
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="CATÁLOGO!A1",
            valueInputOption="RAW",
            body={"values": _clean_rows(rows_activos)},
        )
    )

    # Escribir datos INACTIVOS
    if rows_inactivos:
        _execute_con_reintentos(
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="INACTIVOS!A1",
                valueInputOption="RAW",
                body={"values": _clean_rows(rows_inactivos)},
            )
        )

    # Aplicar formato a CATÁLOGO
    _apply_formatting(sheets_service, spreadsheet_id, sheet_id_catalogo, rows_activos, categorias, cat_rows_activos)

    # Aplicar formato a INACTIVOS (color gris para distinguirla)
    _apply_formatting(sheets_service, spreadsheet_id, sheet_id_inactivos, rows_inactivos, categorias_inact, cat_rows_inactivos,
                      header_color={"red": 0.37, "green": 0.37, "blue": 0.37},
                      cat_color={"red": 0.27, "green": 0.27, "blue": 0.27})

    # Agregar hojas por categoría especial
    _add_category_sheets(sheets_service, spreadsheet_id, datos)

    # Permitir acceso a URLs externas
    _allow_external_url_access(sheets_service, spreadsheet_id)

    # Cargar imágenes como URLs
    _add_image_urls(sheets_service, spreadsheet_id, datos)

    print(f"  ✓ Spreadsheet maestro creado: {spreadsheet_id}")
    print(f"  Activos: {total_activos} | Inactivos: {total_inactivos} (hoja oculta) | Categorías: {len(categorias)}")

    # ─── Registrar cambios y actualizar snapshot ───
    # Guardar snapshot PRIMERO para que sea atómico: si la escritura a Sheets falla,
    # el snapshot refleja el estado ya procesado y no se duplican cambios en la siguiente corrida.
    _save_snapshot(_SNAPSHOT_PATH, datos, datos_inactivos)
    _update_changes_sheet(
        sheets_service, spreadsheet_id, sheet_id_cambios,
        snapshot_anterior, datos, datos_inactivos,
    )

    return spreadsheet_id


def _allow_external_url_access(sheets_service, spreadsheet_id):
    """
    Permite acceso a URLs externas para =IMAGE() e IMPORT*().
    Esto evita el warning 'Limitaciones de seguridad' para los viewers.
    Ref: importFunctionsExternalUrlAccessAllowed property (Sheets API v4).
    """
    try:
        _execute_con_reintentos(
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [{
                        "updateSpreadsheetProperties": {
                            "properties": {
                                "importFunctionsExternalUrlAccessAllowed": True,
                            },
                            "fields": "importFunctionsExternalUrlAccessAllowed",
                        }
                    }]
                },
            )
        )
        print("  ✓ Acceso a URLs externas habilitado (sin warning para viewers)")
    except Exception as e:
        print(f"  ⚠ No se pudo habilitar acceso externo: {e}")


def _add_category_sheets(sheets_service, spreadsheet_id, datos):
    """
    Agrega una hoja WIFI que agrupa las categorías especiales:
    DEXSON, HIKVISION, Tapo, Mercusys Wifi, Dahua Wifi
    """
    SPECIAL_CATS = ["EZVIZ", "IMOU", "TAPO", "MERCUSYS WIFI", "DAHUA WIFI"]

    # Agrupar datos por categoría manteniendo nombre original
    cat_map = {}
    for d in datos:
        cat_upper = d["categoria"].upper()
        if cat_upper in SPECIAL_CATS:
            if cat_upper not in cat_map:
                cat_map[cat_upper] = {"title": d["categoria"], "items": []}
            cat_map[cat_upper]["items"].append(d)

    if not cat_map:
        print("  ⚠ No se encontraron productos para la hoja WIFI")
        return

    # Construir filas combinadas con banners por categoría
    rows = [
        ["", "LISTA DE PRECIOS WIFI - IVA INCLUIDO. SUJETA A CAMBIOS POR DISPONIBILIDAD", "", "", "", "", ""],
        ["", "La información proporcionada no es una propuesta comercial firme", "", "", "", "", ""],
        ["", f"Actualizado: {date.today().strftime('%d/%m/%Y')}", "", "", "", "", ""],
        ["", "", "", "", "", "", ""],
        ["#", "CÓDIGO", "IMAGEN", "NOMBRE", "DISPONIBLE", "DCTO VOL", "VALOR IVA INCLUIDO"],
    ]

    # Agrupar en dict para _apply_formatting
    cats_for_format = {}
    wifi_cat_row_indices = []
    item_num = 1
    for cat_upper in SPECIAL_CATS:
        if cat_upper not in cat_map:
            continue
        title = cat_map[cat_upper]["title"]
        items_sorted = sorted(cat_map[cat_upper]["items"], key=_catalog_item_sort_key)
        cats_for_format[title] = items_sorted
        wifi_cat_row_indices.append(len(rows))
        rows.append(["__CAT__", title, "", "", "", "", ""])
        for d in items_sorted:
            precio_str = f"${d['precio_con_iva']:,.0f}" if d["precio_con_iva"] > 10 else "⚠️ Cotizar"
            disponible = "✔ Disponible" if d["stock"] > 0 else "⚠️ Cotizar"
            dcto = "✓" if d.get("dcto_volumen") else ""
            rows.append([item_num, d["referencia"], "", d["nombre"], disponible, dcto, precio_str])
            item_num += 1

    total = item_num - 1

    # Crear la hoja WIFI como PRIMERA pestaña (index 0)
    result = _execute_con_reintentos(
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": "WIFI",
                "index": 0,
                "gridProperties": {"rowCount": len(rows) + 10, "columnCount": 7, "frozenRowCount": 5},
            }}}]},
        )
    )
    sheet_id = result["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Limpiar tag __CAT__ antes de subir
    rows_clean = [["", r[1], r[2], r[3], r[4], r[5], r[6]] if r[0] == "__CAT__" else r for r in rows]

    # Escribir datos
    _execute_con_reintentos(
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="WIFI!A1",
            valueInputOption="RAW",
            body={"values": rows_clean},
        )
    )

    # Aplicar formato
    _apply_formatting(sheets_service, spreadsheet_id, sheet_id, rows, cats_for_format, wifi_cat_row_indices)

    print(f"  📋 Hoja 'WIFI': {total} productos ({', '.join(cats_for_format.keys())})")


def _apply_formatting(sheets_service, spreadsheet_id, sheet_id, rows, categorias, cat_row_indices,
                      header_color=None, cat_color=None):
    """Aplica formato al spreadsheet usando índices exactos de filas de categoría."""
    if header_color is None:
        header_color = {"red": 0.18, "green": 0.29, "blue": 0.48}
    if cat_color is None:
        cat_color = {"red": 0.12, "green": 0.16, "blue": 0.29}

    NUM_COLS = 7  # A..G (agregada col G = DCTO VOL)

    requests = []

    # Ancho de columnas: #, CÓDIGO, IMAGEN, NOMBRE, DISPONIBLE, DCTO VOL, VALOR IVA
    col_widths = [40, 150, 80, 350, 110, 75, 140]
    for i, width in enumerate(col_widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # Fila 1: Título grande y negrilla
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 14},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment)",
        }
    })

    # Merge fila 1 (B..G)
    requests.append({
        "mergeCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": NUM_COLS},
            "mergeType": "MERGE_ALL",
        }
    })

    # Fila 2: Disclaimer (italic, más pequeño)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"italic": True, "fontSize": 9, "foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}},
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
        }
    })

    # Merge fila 2 (B..G)
    requests.append({
        "mergeCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": NUM_COLS},
            "mergeType": "MERGE_ALL",
        }
    })

    # Fila 5: Headers con fondo azul (A..G)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 4, "endRowIndex": 5, "startColumnIndex": 0, "endColumnIndex": NUM_COLS},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": header_color,
                    "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
        }
    })

    # Banners de categoría usando índices exactos calculados en _build_rows
    for row_idx in cat_row_indices:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1, "startColumnIndex": 0, "endColumnIndex": NUM_COLS},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": cat_color,
                        "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        })
        # Merge del banner: columnas B..G (el nombre de categoría va en col B)
        requests.append({
            "mergeCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1, "startColumnIndex": 1, "endColumnIndex": NUM_COLS},
                "mergeType": "MERGE_ALL",
            }
        })

    # Formato de la columna F (DCTO VOL): ✓ en verde oscuro para filas de datos
    # startRowIndex=5 = primera fila de datos (después de las 5 filas de cabecera)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": len(rows), "startColumnIndex": 5, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": {"red": 0.11, "green": 0.37, "blue": 0.13}},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment)",
        }
    })

    # Formato de la columna G (VALOR IVA INCLUIDO): precio en azul oscuro, negrita
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": len(rows), "startColumnIndex": 6, "endColumnIndex": 7},
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": {"red": 0.12, "green": 0.22, "blue": 0.39}},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment)",
        }
    })

    # Color naranja llamativo para celdas de alerta "⚠️ Cotizar"
    # Se aplica sobre col E (DISPONIBLE, índice 4) y col G (VALOR, índice 6) para filas de datos
    NARANJA = {"red": 1.0, "green": 0.43, "blue": 0.0}   # #FF6E00 — naranja intenso
    NARANJA_TEXTO = {"red": 1.0, "green": 1.0, "blue": 1.0}  # texto blanco sobre naranja
    for col_idx in (4, 6):
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 5, "endRowIndex": len(rows),
                                "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1}],
                    "booleanRule": {
                        "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Cotizar"}]},
                        "format": {
                            "backgroundColor": NARANJA,
                            "textFormat": {"bold": True, "foregroundColor": NARANJA_TEXTO},
                        },
                    },
                },
                "index": 0,
            }
        })

    # Altura de filas de datos (45px para imágenes)
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 5, "endIndex": len(rows)},
            "properties": {"pixelSize": 45},
            "fields": "pixelSize",
        }
    })

    if requests:
        _execute_con_reintentos(
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
        )


def _add_image_urls(sheets_service, spreadsheet_id, datos):
    """Agrega fórmulas IMAGE() para mostrar imágenes desde URLs públicas."""
    # Cargar URLs de imágenes si existen
    urls_file = os.path.join(BASE_DIR, "data", "images_urls.json")
    if not os.path.exists(urls_file):
        return

    try:
        with open(urls_file, "r", encoding="utf-8") as f:
            image_urls = json.load(f)
    except (json.JSONDecodeError, IOError):
        return

    if not image_urls:
        return

    def _convert_url(url: str) -> str:
        """Convierte URL de Cloudinary para evitar comas (conflicto con locale es_CO en Sheets)."""
        # Cloudinary acepta transformaciones separadas por / en vez de ,
        # Ej: /w_200,h_200,c_limit,q_auto/ → /w_200/h_200/c_limit/q_auto/
        if "cloudinary" in url and "/upload/" in url:
            parts = url.split("/upload/")
            if len(parts) == 2:
                after_upload = parts[1]
                # Separar transformaciones del public_id
                segments = after_upload.split("/")
                new_segments = []
                for seg in segments:
                    if "," in seg and "_" in seg:
                        # Es un grupo de transformaciones como "w_200,h_200,c_limit,q_auto"
                        new_segments.extend(seg.split(","))
                    else:
                        new_segments.append(seg)
                return parts[0] + "/upload/" + "/".join(new_segments)
        return url

    products_by_reference = {str(item.get("referencia") or "").strip(): item for item in datos}

    def _build_image_updates(sheet_name, products):
        products_by_reference = {
            str(item.get("referencia") or "").strip(): item for item in products
        }
        rows = _execute_con_reintentos(
            sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A6:B",
            )
        ).get("values", [])
        updates = []
        for offset, row in enumerate(rows):
            if len(row) < 2 or not str(row[0]).strip().isdigit():
                continue
            item = products_by_reference.get(str(row[1] or "").strip())
            if item is None:
                continue
            url = image_urls.get(str(item.get("id") or ""), "")
            if not url:
                continue
            updates.append({
                "range": f"'{sheet_name}'!C{6 + offset}",
                "values": [[f'=IMAGE("{_convert_url(url)}"; 1)']],
            })
        if updates:
            _execute_con_reintentos(
                sheets_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": updates},
                )
            )
        return len(updates)

    catalog_count = _build_image_updates("CATÁLOGO", datos)
    print(f"  🖼️ {catalog_count} imágenes agregadas al spreadsheet")

    # Imágenes para hoja WIFI (categorías especiales unificadas)
    SPECIAL_CATS = ["EZVIZ", "IMOU", "TAPO", "MERCUSYS WIFI", "DAHUA WIFI"]
    wifi_products = [item for item in datos if item["categoria"].upper() in SPECIAL_CATS]
    wifi_count = _build_image_updates("WIFI", wifi_products)
    print(f"  🖼️ {wifi_count} imágenes agregadas a hoja WIFI")
