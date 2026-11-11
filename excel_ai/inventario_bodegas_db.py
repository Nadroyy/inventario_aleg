"""
inventario_bodegas_db.py
Genera el Excel de inventario por bodega leyendo desde MySQL local
(inventario_local) y actualiza el archivo Excel INVENTARIO_BODEGAS_ALIANXA en Drive.

Mantiene la estructura del archivo Excel compartido en Drive:
  A=REFERENCIA | B=NOMBRE
  C/D/E = Principal (Stock / Remis. / Real)
  F/G/H = Caobos    (Stock / Remis. / Real)
  I/J/K = Bulevar   (Stock / Remis. / Real)
  L=INSTALADOR | M=CLIENTE | N=DCTO VOL

Remis. viene de tabla stock.stock_minimo (campo reutilizado como remisiones)
si en un futuro se agrega una tabla de remisiones, solo hay que cambiar la query.

Uso:
    python -m excel_ai.inventario_bodegas_db
    python -m excel_ai.inventario_bodegas_db --solo-excel   # no sube a Drive
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from googleapiclient.http import MediaFileUpload
from proyecto_ecxel.share_catalog import get_drive_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ─── Configuración ────────────────────────────────────────
DRIVE_FILE_ID = os.getenv("INVENTORY_BODEGAS_DRIVE_FILE_ID", "")   # INVENTARIO_BODEGAS_ALIANXA1
OUTPUT_FILE   = PROJECT_ROOT / "excel_ai" / "inventario_bodegas_db.xlsx"

BODEGAS = ["Principal", "Caobos", "Bulevar"]   # orden de columnas

DB_CONFIG = {
    "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER",     "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "inventario_local"),
    "charset":  "utf8mb4",
}


# ════════════════════════════════════════════════════════════
# 1) Lectura de datos desde MySQL
# ════════════════════════════════════════════════════════════

def _load_from_db() -> list[dict]:
    """
    Retorna lista de productos con su stock por bodega y precios.
    Estructura por producto:
      {ref, nombre, categoria, precio_instalador, precio_cliente,
       dcto_volumen,
       stock: {Principal: {available, remissioned, real}, ...}}
    """
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    # Productos activos con todos los campos (excepto ruta_imagen y moneda)
    cur.execute("""
        SELECT
            p.id_product,
            p.ref_producto      AS ref,
            p.nombre,
            p.descripcion,
            p.marca,
            p.tipo,
            p.estado,
            p.ocultar_tienda,
            p.categoria,
            pa.precio AS precio_aliados,
            pi.precio AS precio_instalador,
            pc.precio AS precio_cliente,
            p.escala_descuentos,
            p.fecha_creacion
        FROM productos p
        LEFT JOIN al2.precio_producto pa
          ON pa.id_producto = p.id_product AND pa.id_lista_precio = 3
         AND pa.fecha_actualizacion = (
             SELECT MAX(x.fecha_actualizacion) FROM al2.precio_producto x
             WHERE x.id_producto = p.id_product AND x.id_lista_precio = 3
         )
        LEFT JOIN al2.precio_producto pi
          ON pi.id_producto = p.id_product AND pi.id_lista_precio = 1
         AND pi.fecha_actualizacion = (
             SELECT MAX(x.fecha_actualizacion) FROM al2.precio_producto x
             WHERE x.id_producto = p.id_product AND x.id_lista_precio = 1
         )
        LEFT JOIN al2.precio_producto pc
          ON pc.id_producto = p.id_product AND pc.id_lista_precio = 2
         AND pc.fecha_actualizacion = (
             SELECT MAX(x.fecha_actualizacion) FROM al2.precio_producto x
             WHERE x.id_producto = p.id_product AND x.id_lista_precio = 2
         )
        WHERE p.estado = 'activo'
          AND p.ref_producto IS NOT NULL
          AND p.ref_producto != ''
        ORDER BY p.categoria, p.ref_producto
    """)
    productos = cur.fetchall()

    # Stock por bodega (join con warehouses)
    cur.execute("""
        SELECT
            s.id_product,
            w.name          AS bodega,
            s.disponible    AS available,
            s.stock_minimo  AS remissioned
        FROM stock s
        JOIN warehouses w ON s.id_warehouse = w.id_warehouse
        WHERE w.name IN ('Principal', 'Caobos', 'Bulevar')
    """)
    stock_rows = cur.fetchall()
    cur.close()
    conn.close()

    # Indexar stock por producto
    stock_map: dict[str, dict] = {}
    for sr in stock_rows:
        pid = sr["id_product"]
        if pid not in stock_map:
            stock_map[pid] = {}
        av  = int(sr["available"]   or 0)
        rem = int(sr["remissioned"] or 0)
        stock_map[pid][sr["bodega"]] = {
            "available":   av,
            "remissioned": rem,
            "real":        av - rem,
        }

    # Combinar
    result = []
    for p in productos:
        pid  = p["id_product"]
        s    = stock_map.get(pid, {})

        # Determinar si tiene descuento por volumen
        dcto = False
        precio_aliados = p["precio_aliados"]
        precio_instalador = p["precio_instalador"]
        if (
            precio_aliados is not None
            and precio_instalador is not None
            and precio_aliados < precio_instalador
        ):
            dcto = True
        if not dcto and p["escala_descuentos"]:
            try:
                escalas = json.loads(p["escala_descuentos"]) if isinstance(p["escala_descuentos"], str) else p["escala_descuentos"]
                if escalas:
                    dcto = True
            except Exception:
                pass

        result.append({
            "ref":               p["ref"],
            "nombre":            p["nombre"],
            "descripcion":       p["descripcion"] or "",
            "marca":             p["marca"] or "",
            "tipo":              p["tipo"] or "",
            "estado":            p["estado"] or "",
            "ocultar_tienda":    bool(p["ocultar_tienda"]),
            "categoria":         (p["categoria"] or "OTROS").upper(),
            "precio_aliados":    float(p["precio_aliados"])    if p["precio_aliados"]    else None,
            "precio_instalador": float(p["precio_instalador"]) if p["precio_instalador"] else None,
            "precio_cliente":    float(p["precio_cliente"])    if p["precio_cliente"]    else None,
            "escala_descuentos": p["escala_descuentos"] or "",
            "fecha_creacion":    str(p["fecha_creacion"]) if p["fecha_creacion"] else "",
            "dcto_volumen":      dcto,
            "stock":             {b: s.get(b, {"available": 0, "remissioned": 0, "real": 0}) for b in BODEGAS},
        })

    return result


# ════════════════════════════════════════════════════════════
# 2) Generación del Excel local (misma estructura)
# ════════════════════════════════════════════════════════════

def _generate_excel(productos: list[dict]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    # Agrupar por categoría
    cats: dict[str, list] = {}
    for p in productos:
        cat = p["categoria"]
        cats.setdefault(cat, []).append(p)

    thin = Border(
        left=Side(style="thin",   color="DDDDDD"),
        right=Side(style="thin",  color="DDDDDD"),
        top=Side(style="thin",    color="DDDDDD"),
        bottom=Side(style="thin", color="DDDDDD"),
    )
    hdr_font    = Font(bold=True, size=9,  color="FFFFFF")
    hdr_fill    = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
    bodega_fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    no_fill     = PatternFill(fill_type=None)
    cat_font    = Font(bold=True, size=10, color="FFFFFF")
    cat_fill    = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    data_font   = Font(size=9)
    bold_font   = Font(size=9,  bold=True)
    zero_font   = Font(size=9,  color="BBBBBB")
    red_font    = Font(size=9,  bold=True, color="C62828")
    orange_font = Font(size=9,  bold=True, color="E65100")
    price_font  = Font(size=9,  bold=True)
    green_font  = Font(size=10, bold=True, color="1B5E20")

    wb = Workbook()
    ws = wb.active
    ws.title = "INVENTARIO"

    # Layout columnas A..V (22 columnas):
    # A=REF  B=NOMBRE  C=DESCRIPCION  D=MARCA  E=TIPO  F=ESTADO  G=OCULTAR_TIENDA
    # H=P.ALIADOS  I=P.INSTALADOR  J=P.CLIENTE  K=DCTO_VOL%  L=ESCALA_DESCUENTOS  M=FECHA_CREACION
    # N/O/P=Principal  Q/R/S=Caobos  T/U/V=Bulevar  W=DCTO_VOL✓
    TOTAL_COLS = 23  # A..W

    # ── Fila 1: Headers ──────────────────────────────────
    headers = [
        "REFERENCIA", "NOMBRE", "DESCRIPCIÓN", "MARCA", "TIPO",
        "ESTADO", "OCULTAR TIENDA",
        "P. ALIADOS", "P. INSTALADOR", "P. CLIENTE", "DCTO VOL %", "ESCALA DESCUENTOS", "FECHA CREACIÓN",
        "Stock", "Remis.", "Real",   # Principal N-P
        "Stock", "Remis.", "Real",   # Caobos    Q-S
        "Stock", "Remis.", "Real",   # Bulevar   T-V
        "DCTO VOL",
    ]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hdr_font; cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin
    ws.row_dimensions[1].height = 28

    # ── Fila 2: nombres de bodegas (solo las columnas de stock) ──
    for c in range(1, 14):  # A..M sin bodegas
        ws.cell(row=2, column=c).fill = hdr_fill
        ws.cell(row=2, column=c).border = thin

    for name, sc in {"PRINCIPAL": 14, "CAOBOS": 17, "BULEVAR": 20}.items():
        ws.merge_cells(start_row=2, start_column=sc, end_row=2, end_column=sc + 2)
        cell = ws.cell(row=2, column=sc, value=name)
        cell.font = Font(bold=True, size=9, color="FFFFFF")
        cell.fill = bodega_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin

    # Celda DCTO VOL en fila 2
    cell = ws.cell(row=2, column=23, value="DCTO VOL")
    cell.font = Font(bold=True, size=9, color="1F3864")
    cell.fill = no_fill
    cell.alignment = Alignment(horizontal="center", vertical="center")
    cell.border = thin

    ws.row_dimensions[2].height = 14

    # ── Datos ────────────────────────────────────────────
    row = 3
    COP_FMT = '"$"#,##0'

    for cat in sorted(cats.keys()):
        # Banner categoría
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=TOTAL_COLS)
        cell = ws.cell(row=row, column=1, value=cat)
        cell.font = cat_font; cell.fill = cat_fill
        cell.alignment = Alignment(horizontal="center"); cell.border = thin
        ws.row_dimensions[row].height = 15
        row += 1

        for p in sorted(cats[cat], key=lambda x: x["ref"]):
            # A: Referencia
            c = ws.cell(row=row, column=1, value=p["ref"])
            c.font = bold_font; c.alignment = Alignment(horizontal="center"); c.border = thin

            # B: Nombre
            c = ws.cell(row=row, column=2, value=p["nombre"])
            c.font = data_font; c.border = thin

            # C: Descripción
            c = ws.cell(row=row, column=3, value=p["descripcion"])
            c.font = Font(size=8); c.border = thin; c.alignment = Alignment(wrap_text=True)

            # D: Marca
            c = ws.cell(row=row, column=4, value=p["marca"])
            c.font = data_font; c.border = thin; c.alignment = Alignment(horizontal="center")

            # E: Tipo
            c = ws.cell(row=row, column=5, value=p["tipo"])
            c.font = data_font; c.border = thin; c.alignment = Alignment(horizontal="center")

            # F: Estado
            c = ws.cell(row=row, column=6, value=p["estado"])
            c.font = Font(size=9, bold=True,
                          color="1B5E20" if p["estado"] == "activo" else "C62828")
            c.border = thin; c.alignment = Alignment(horizontal="center")

            # G: Ocultar tienda
            c = ws.cell(row=row, column=7, value="Sí" if p["ocultar_tienda"] else "No")
            c.font = data_font; c.border = thin; c.alignment = Alignment(horizontal="center")

            # H: Precio aliados
            val = p["precio_aliados"]
            c = ws.cell(row=row, column=8, value=val if val else "")
            c.font = price_font if val else zero_font
            c.border = thin; c.alignment = Alignment(horizontal="right")
            if val: c.number_format = COP_FMT

            # I: Precio instalador
            val = p["precio_instalador"]
            c = ws.cell(row=row, column=9, value=val if val else "")
            c.font = price_font if val else zero_font
            c.border = thin; c.alignment = Alignment(horizontal="right")
            if val: c.number_format = COP_FMT

            # J: Precio cliente
            val = p["precio_cliente"]
            c = ws.cell(row=row, column=10, value=val if val else "")
            c.font = price_font if val else zero_font
            c.border = thin; c.alignment = Alignment(horizontal="right")
            if val: c.number_format = COP_FMT

            # K: Descuento volumen %
            val = p["dcto_volumen"]
            c = ws.cell(row=row, column=11, value=val if val else "")
            c.font = data_font if val else zero_font
            c.border = thin; c.alignment = Alignment(horizontal="center")

            # L: Escala descuentos
            c = ws.cell(row=row, column=12, value=str(p["escala_descuentos"]) if p["escala_descuentos"] else "")
            c.font = Font(size=8); c.border = thin

            # M: Fecha creación
            c = ws.cell(row=row, column=13, value=p["fecha_creacion"])
            c.font = Font(size=8); c.border = thin; c.alignment = Alignment(horizontal="center")

            # N-V: Stock por bodega (Principal N-P, Caobos Q-S, Bulevar T-V)
            col = 14
            for bodega in BODEGAS:
                s   = p["stock"][bodega]
                av  = s["available"]
                rem = s["remissioned"]
                rl  = s["real"]

                c_av = ws.cell(row=row, column=col, value=av if av else "")
                c_av.font = bold_font if av > 0 else zero_font
                c_av.alignment = Alignment(horizontal="center"); c_av.border = thin

                c_re = ws.cell(row=row, column=col + 1, value=rem if rem else "")
                c_re.font = red_font if rem > 0 else zero_font
                c_re.alignment = Alignment(horizontal="center"); c_re.border = thin

                c_rl = ws.cell(row=row, column=col + 2, value=rl if rl > 0 else "")
                c_rl.font = (orange_font if rl > 0 and rem > 0
                             else bold_font if rl > 0 else zero_font)
                c_rl.alignment = Alignment(horizontal="center"); c_rl.border = thin
                col += 3

            # W: DCTO VOL ✓
            c_d = ws.cell(row=row, column=23, value="✓" if p["dcto_volumen"] else "")
            c_d.font = green_font if p["dcto_volumen"] else zero_font
            c_d.alignment = Alignment(horizontal="center", vertical="center"); c_d.border = thin

            ws.row_dimensions[row].height = 40  # más alto por descripción
            row += 1

    # ── Anchos de columna ────────────────────────────────
    ws.column_dimensions["A"].width = 22   # REFERENCIA
    ws.column_dimensions["B"].width = 40   # NOMBRE
    ws.column_dimensions["C"].width = 50   # DESCRIPCIÓN
    ws.column_dimensions["D"].width = 14   # MARCA
    ws.column_dimensions["E"].width = 14   # TIPO
    ws.column_dimensions["F"].width = 10   # ESTADO
    ws.column_dimensions["G"].width = 12   # OCULTAR TIENDA
    ws.column_dimensions["H"].width = 14   # P. ALIADOS
    ws.column_dimensions["I"].width = 14   # P. INSTALADOR
    ws.column_dimensions["J"].width = 14   # P. CLIENTE
    ws.column_dimensions["K"].width = 10   # DCTO VOL %
    ws.column_dimensions["L"].width = 30   # ESCALA DESCUENTOS
    ws.column_dimensions["M"].width = 18   # FECHA CREACIÓN
    for letter in ["N","O","P","Q","R","S","T","U","V"]:
        ws.column_dimensions[letter].width = 7
    ws.column_dimensions["W"].width = 9    # DCTO VOL ✓

    ws.freeze_panes = "A3"
    wb.save(str(OUTPUT_FILE))
    print(f"  ✓ Excel generado: {OUTPUT_FILE}")
    print(f"  Productos: {row - 3 - len(cats)} | Categorías: {len(cats)}")
    return OUTPUT_FILE


# ════════════════════════════════════════════════════════════
# 3) Actualizar el archivo Excel compartido en Drive
# ════════════════════════════════════════════════════════════

def _upload_to_drive():
    """Reemplaza el contenido del archivo XLSX compartido sin cambiar su enlace."""
    if not DRIVE_FILE_ID:
        raise RuntimeError(
            "Set INVENTORY_BODEGAS_DRIVE_FILE_ID before uploading to Drive."
        )
    drive = get_drive_service()
    media = MediaFileUpload(
        str(OUTPUT_FILE),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=True,
    )
    file = drive.files().update(
        fileId=DRIVE_FILE_ID,
        media_body=media,
        fields="id,name,mimeType,webViewLink",
    ).execute()

    print(f"  ✓ Archivo Excel actualizado en Drive: {file.get('webViewLink', '')}")


# ════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════

def main():
    solo_excel = "--solo-excel" in sys.argv

    if not solo_excel:
        from actualizar_catalogo import LOCK_FILE, _acquire_run_lock

        _acquire_run_lock(LOCK_FILE, wait=True)

    print("Cargando datos desde MySQL local...")
    productos = _load_from_db()
    print(f"  ✓ {len(productos)} productos activos cargados\n")

    print("Generando Excel...")
    _generate_excel(productos)

    if not solo_excel:
        print("\nActualizando el archivo Excel compartido en Drive...")
        try:
            _upload_to_drive()
        except Exception as e:
            print(f"  ⚠ No se pudo actualizar el archivo en Drive: {e}")
            print("  El Excel local sí está listo; la actualización de Drive falló.")
            raise

    print("\n✓ Listo.")


if __name__ == "__main__":
    main()
