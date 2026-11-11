"""
Genera catálogo Excel con imágenes embebidas.
Columnas: #, CÓDIGO, IMAGEN, NOMBRE, VALOR IVA INCLUIDO, DISPONIBLE
Precio: INSTALADOR + IVA 19%
Stock: Unificado (✔ Disponible / ✗ Agotado)
"""

import json
import os
from datetime import date

# Bodegas que cuentan como stock vendible. Se ignora cualquier otra
# (p. ej. "Baja de Mercancia", que es mercancía descartada y no debe
# sumar a la disponibilidad del catálogo).
BODEGAS_VALIDAS = {"principal", "bulevar", "caobos"}


def is_reportable_product(item):
    """Indica si un item es producto, tiene referencia y está visible en tienda."""
    return (
        item.get("type") == "product"
        and bool(str(item.get("reference") or "").strip())
        and (item.get("settingsOnShop") or {}).get("hide") is not True
    )


def load_items(filename="data/items.json"):
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_catalog_data(item):
    """Extrae datos relevantes para el catálogo."""
    # Precio INSTALADOR y ALIA2
    precio = 0
    precio_alia2 = None
    for p in item.get("price", []):
        if not isinstance(p, dict):
            continue
        name_upper = (p.get("name") or "").upper()
        if "INSTALADOR" in name_upper:
            precio = p.get("price", 0) or 0
        elif "ALIA2" in name_upper:
            precio_alia2 = p.get("price") or None
    if precio == 0:
        pl = item.get("price", [])
        if pl and isinstance(pl, list):
            precio = pl[0].get("price", 0) or 0

    # IVA
    iva = 19.0
    for t in item.get("tax", []):
        if isinstance(t, dict):
            iva = float(t.get("percentage", 19))
            break
    precio_con_iva = round(precio * (1 + iva / 100)) if precio > 0 else 0

    # Stock real = disponible - remisionado, sumando SOLO las bodegas válidas
    # (Principal, Bulevar, Caobos). Las remisiones abiertas apartan unidades que
    # ya no están disponibles. Bodegas como "Baja de Mercancia" se excluyen.
    inventory = item.get("inventory", {})
    stock = 0
    if isinstance(inventory, dict):
        warehouses = inventory.get("warehouses", []) or []
        if warehouses:
            for w in warehouses:
                if not isinstance(w, dict):
                    continue
                nombre = str(w.get("name") or "").strip().lower()
                if nombre not in BODEGAS_VALIDAS:
                    continue
                av = int(w.get("availableQuantity") or 0)
                rem = int(w.get("remissionedQuantity") or 0)
                stock += (av - rem)
            stock = max(stock, 0)
        else:
            stock = int(inventory.get("availableQuantity", 0) or 0)

    # Categoría
    ic = item.get("itemCategory", {})
    categoria = ic.get("name", "OTROS") if isinstance(ic, dict) else "OTROS"

    # Descuento por volumen:
    # Condición 1: customField escala_descuentos con valor no vacío.
    #   (Es un string tipo "4:2.5-7:4.5" = cantidad:porcentaje por tramos.)
    # Condición 2: tiene precio ALIA2 menor al INSTALADOR
    dcto_volumen = False
    for cf in (item.get("customFields", []) or []):
        if cf.get("name") == "escala_descuentos" and str(cf.get("value") or "").strip():
            dcto_volumen = True
            break
    if not dcto_volumen and precio_alia2 and precio > 0:
        if precio_alia2 < precio:
            dcto_volumen = True

    return {
        "id": str(item.get("id", "")),
        "nombre": item.get("name", ""),
        "referencia": item.get("reference", "") or "",
        "categoria": categoria.upper(),
        "precio_con_iva": precio_con_iva,
        "stock": stock,
        "dcto_volumen": dcto_volumen,
    }


def generate_catalog_excel(items, filename="data/catalogo_alianxa.xlsx", solo_con_stock=False):
    """Genera Excel limpio con imágenes embebidas.
    
    - Hoja CATÁLOGO: productos activos (visible)
    - Hoja INACTIVOS: productos inactivos (oculta, para referencia)
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.drawing.image import Image as XLImage
    except ImportError:
        os.system("py -m pip install openpyxl Pillow --quiet")
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.drawing.image import Image as XLImage

    # Separar activos e inactivos
    activos   = [item for item in items if item.get("type") == "product" and item.get("status") == "active"]
    inactivos = [item for item in items if item.get("type") == "product" and item.get("status") != "active"]

    datos_activos   = [extract_catalog_data(item) for item in activos]
    datos_inactivos = [extract_catalog_data(item) for item in inactivos]

    if solo_con_stock:
        datos_activos = [d for d in datos_activos if d["stock"] > 0]

    # Estilos reutilizables
    header_font    = Font(bold=True, size=10, color="FFFFFF")
    header_fill    = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    cat_font       = Font(bold=True, size=12, color="FFFFFF")
    cat_fill       = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    data_font      = Font(size=9)
    price_font     = Font(size=11, bold=True, color="1F3864")
    disponible_font= Font(size=10, bold=True, color="2E7D32")
    agotado_font   = Font(size=10, bold=True, color="C62828")
    inact_cat_fill = PatternFill(start_color="616161", end_color="616161", fill_type="solid")
    border_thin    = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        top=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC'),
    )

    img_dir = "data/images"

    wb = Workbook()

    # ══════════════════════════════════════════
    # HOJA 1: CATÁLOGO (activos, visible)
    # ══════════════════════════════════════════
    ws = wb.active
    ws.title = "CATÁLOGO"

    def _write_sheet(ws, datos, titulo_fill, titulo_cat_fill, oculta=False):
        """Escribe los datos en la hoja dada. Retorna el número de productos escritos."""
        # Agrupar por categoría
        categorias = {}
        for d in datos:
            cat = d["categoria"]
            if cat not in categorias:
                categorias[cat] = []
            categorias[cat].append(d)
        categorias = dict(sorted(categorias.items()))

        # ── Encabezado global ──
        row = 1
        ws.cell(row=row, column=2, value="LISTA DE PRECIOS - IVA INCLUIDO. SUJETA A CAMBIOS POR DISPONIBILIDAD")
        ws.cell(row=row, column=2).font = Font(bold=True, size=14, color="1F3864")
        ws.merge_cells("B1:E1")
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="center", vertical="center")

        row = 2
        ws.cell(row=row, column=2, value="La información proporcionada no es una propuesta comercial firme")
        ws.cell(row=row, column=2).font = Font(italic=True, size=9, color="666666")
        ws.merge_cells("B2:E2")
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="center")

        row = 3
        ws.cell(row=row, column=2, value=f"Actualizado: {date.today().strftime('%d/%m/%Y')}")
        ws.cell(row=row, column=2).font = Font(size=10, color="333333")
        ws.merge_cells("B3:C3")
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="center")

        # ── Headers de columna ──
        row = 4
        headers = ["#", "CÓDIGO", "IMAGEN", "NOMBRE", "VALOR IVA INCLUIDO", "DISPONIBLE"]
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=row, column=col, value=h)
            cell.font = header_font
            cell.fill = titulo_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border_thin

        row = 5
        item_num = 1

        for categoria, items_cat in categorias.items():
            # Banner categoría
            for col in range(1, 7):
                ws.cell(row=row, column=col).fill = titulo_cat_fill
                ws.cell(row=row, column=col).border = border_thin
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            cell = ws.cell(row=row, column=2, value=categoria)
            cell.font = cat_font
            cell.fill = titulo_cat_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[row].height = 25
            row += 1

            for d in sorted(items_cat, key=lambda x: (x["nombre"][0].upper(), x["precio_con_iva"])):
                # #
                ws.cell(row=row, column=1, value=item_num).font = data_font
                ws.cell(row=row, column=1).alignment = Alignment(horizontal="center", vertical="center")
                ws.cell(row=row, column=1).border = border_thin

                # CÓDIGO
                ws.cell(row=row, column=2, value=d["referencia"]).font = Font(size=9, bold=True)
                ws.cell(row=row, column=2).alignment = Alignment(horizontal="center", vertical="center")
                ws.cell(row=row, column=2).border = border_thin

                # IMAGEN
                ws.cell(row=row, column=3).border = border_thin
                if os.path.isdir(img_dir):
                    for ext in [".jpg", ".png", ".gif", ".webp"]:
                        img_path = os.path.join(img_dir, f"{d['id']}{ext}")
                        if os.path.exists(img_path):
                            try:
                                img = XLImage(img_path)
                                img.width = 55
                                img.height = 55
                                ws.add_image(img, f"C{row}")
                            except Exception:
                                pass
                            break

                # NOMBRE
                ws.cell(row=row, column=4, value=d["nombre"]).font = Font(size=10, bold=True)
                ws.cell(row=row, column=4).alignment = Alignment(vertical="center", wrap_text=True)
                ws.cell(row=row, column=4).border = border_thin

                # VALOR IVA
                if d["precio_con_iva"] > 10:
                    ws.cell(row=row, column=5, value=d["precio_con_iva"]).font = price_font
                    ws.cell(row=row, column=5).number_format = '$#,##0'
                else:
                    ws.cell(row=row, column=5, value="CONTACTAR").font = Font(size=9, italic=True, color="666666")
                ws.cell(row=row, column=5).alignment = Alignment(horizontal="center", vertical="center")
                ws.cell(row=row, column=5).border = border_thin

                # DISPONIBLE
                if d["stock"] > 0:
                    ws.cell(row=row, column=6, value="✔ Disponible").font = disponible_font
                else:
                    ws.cell(row=row, column=6, value="✗ Agotado").font = agotado_font
                ws.cell(row=row, column=6).alignment = Alignment(horizontal="center", vertical="center")
                ws.cell(row=row, column=6).border = border_thin

                ws.row_dimensions[row].height = 45
                item_num += 1
                row += 1

        # Anchos
        ws.column_dimensions['A'].width = 5
        ws.column_dimensions['B'].width = 20
        ws.column_dimensions['C'].width = 10
        ws.column_dimensions['D'].width = 50
        ws.column_dimensions['E'].width = 18
        ws.column_dimensions['F'].width = 14

        ws.freeze_panes = "A5"

        # Protección
        ws.protection.sheet = True
        ws.protection.password = os.getenv("EXCEL_SHEET_PASSWORD", "")

        # Ocultar hoja si corresponde
        if oculta:
            ws.sheet_state = "hidden"

        return item_num - 1

    # Escribir CATÁLOGO (activos)
    total_activos = _write_sheet(ws, datos_activos, header_fill, cat_fill, oculta=False)

    # ══════════════════════════════════════════
    # HOJA 2: INACTIVOS (oculta)
    # ══════════════════════════════════════════
    ws_inact = wb.create_sheet(title="INACTIVOS")
    total_inactivos = _write_sheet(ws_inact, datos_inactivos, 
        PatternFill(start_color="757575", end_color="757575", fill_type="solid"),
        inact_cat_fill, oculta=True)

    # Guardar
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    wb.save(filename)

    print(f"\n{'═'*60}")
    print(f" CATÁLOGO GENERADO EXITOSAMENTE")
    print(f"{'═'*60}")
    print(f"  Archivo:     {filename}")
    print(f"  Activos:     {total_activos}")
    print(f"  Inactivos:   {total_inactivos} (hoja oculta)")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    solo_stock = "--stock" in args
    items = load_items()
    generate_catalog_excel(items, solo_con_stock=solo_stock)
