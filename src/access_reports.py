"""Consultas de solo lectura para el MVP de analítica de accesos."""

from __future__ import annotations

import os
from io import BytesIO
from datetime import date, datetime, time, timedelta
from typing import Any

import mysql.connector
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

load_dotenv()

_CLIENT_KEY_SQL = """
COALESCE(
    NULLIF(TRIM(documento), ''),
    NULLIF(TRIM(telefono), ''),
    NULLIF(LOWER(TRIM(email)), ''),
    CONCAT('acceso:', id)
)
"""


def build_access_filters(
    start_date: date,
    end_date: date,
    origin: str | None = None,
) -> tuple[str, list[object]]:
    """Construye filtros parametrizados; la fecha final es inclusiva."""
    clauses = ["fecha >= %s", "fecha < %s"]
    params: list[object] = [
        datetime.combine(start_date, time.min),
        datetime.combine(end_date + timedelta(days=1), time.min),
    ]
    if origin:
        clauses.append("origen = %s")
        params.append(origin)
    return " AND ".join(clauses), params


def _db_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("REPORTS_DB_USER") or os.getenv("MYSQL_USER", "root"),
        password=os.getenv("REPORTS_DB_PASSWORD") or os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "inventario_local"),
        charset="utf8mb4",
        connection_timeout=10,
    )


def fetch_access_report(
    start_date: date,
    end_date: date,
    origin: str | None = None,
) -> dict:
    """Devuelve métricas agregadas y listados sin datos de contacto."""
    where_sql, params = build_access_filters(start_date, end_date, origin)
    conn = _db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS total_accesses,
                COUNT(DISTINCT client_key) AS unique_clients,
                COUNT(DISTINCT CASE
                    WHEN alegra_registrado = 0 THEN client_key
                END) AS new_clients,
                COUNT(DISTINCT CASE
                    WHEN accesses_per_client >= 2 THEN client_key
                END) AS frequent_clients
            FROM (
                SELECT
                    client_key,
                    alegra_registrado,
                    COUNT(*) OVER (PARTITION BY client_key) AS accesses_per_client
                FROM (
                    SELECT
                        {_CLIENT_KEY_SQL} AS client_key,
                        alegra_registrado
                    FROM registro_accesos
                    WHERE {where_sql}
                ) filtered_accesses
            ) client_accesses
            """,
            params,
        )
        summary = cursor.fetchone()

        cursor.execute(
            f"""
            SELECT origen, COUNT(*) AS accesses
            FROM registro_accesos
            WHERE {where_sql}
            GROUP BY origen
            ORDER BY accesses DESC, origen
            """,
            params,
        )
        by_origin = cursor.fetchall()

        cursor.execute(
            f"""
            SELECT
                client_key,
                MAX(NULLIF(TRIM(nombre), '')) AS name,
                SUBSTRING_INDEX(
                    GROUP_CONCAT(NULLIF(TRIM(telefono), '') ORDER BY fecha DESC SEPARATOR '|||'),
                    '|||',
                    1
                ) AS phone,
                COUNT(*) AS accesses,
                MAX(fecha) AS last_access,
                GROUP_CONCAT(DISTINCT origen ORDER BY origen SEPARATOR ', ') AS origins
            FROM (
                SELECT
                    {_CLIENT_KEY_SQL} AS client_key,
                    nombre,
                    telefono,
                    origen,
                    fecha
                FROM registro_accesos
                WHERE {where_sql}
            ) filtered_accesses
            GROUP BY client_key
            HAVING COUNT(*) >= 2
            ORDER BY accesses DESC, last_access DESC
            """,
            params,
        )
        frequent_clients = cursor.fetchall()

        cursor.execute(
            f"""
            SELECT
                client_key,
                MAX(NULLIF(TRIM(nombre), '')) AS name,
                SUBSTRING_INDEX(
                    GROUP_CONCAT(NULLIF(TRIM(telefono), '') ORDER BY fecha DESC SEPARATOR '|||'),
                    '|||',
                    1
                ) AS phone,
                COUNT(*) AS accesses,
                MAX(fecha) AS last_access
            FROM (
                SELECT
                    {_CLIENT_KEY_SQL} AS client_key,
                    nombre,
                    telefono,
                    fecha
                FROM registro_accesos
                WHERE {where_sql} AND alegra_registrado = 0
            ) new_accesses
            GROUP BY client_key
            ORDER BY last_access DESC
            """,
            params,
        )
        new_clients = cursor.fetchall()

        return {
            "summary": summary,
            "by_origin": by_origin,
            "frequent_clients": frequent_clients,
            "new_clients": new_clients,
        }
    finally:
        cursor.close()
        conn.close()


def build_access_report_xlsx(
    report: dict[str, Any],
    start_date: date,
    end_date: date,
    origin: str | None = None,
) -> bytes:
    """Construye un libro con resumen visual y listados de seguimiento."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Resumen de accesos"
    sheet.sheet_view.showGridLines = False

    navy = "1A2A4A"
    blue = "176FA5"
    pale_blue = "EAF2F8"
    light = "F3F6FA"
    gray = "637083"
    white = "FFFFFF"
    border = Side(style="thin", color="DCE3EB")

    sheet.merge_cells("A1:H1")
    sheet["A1"] = "REPORTE DE ACCESOS AL CATÁLOGO"
    sheet["A1"].font = Font(name="Aptos Display", size=20, bold=True, color=white)
    sheet["A1"].fill = PatternFill("solid", fgColor=navy)
    sheet["A1"].alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 38
    for row in sheet["A1:H1"]:
        for cell in row:
            cell.fill = PatternFill("solid", fgColor=navy)

    sheet.merge_cells("A2:H2")
    sheet["A2"] = (
        f"Período: {start_date.strftime('%d/%m/%Y')} – {end_date.strftime('%d/%m/%Y')}"
        f"    |    Origen: {origin.capitalize() if origin else 'Todos'}"
    )
    sheet["A2"].font = Font(size=11, color=gray, italic=True)
    sheet["A2"].alignment = Alignment(vertical="center")
    sheet.row_dimensions[2].height = 25

    metrics = [
        ("A4:B4", "A5:B6", "ACCESOS EXITOSOS", report["summary"]["total_accesses"]),
        ("C4:D4", "C5:D6", "PERSONAS ÚNICAS", report["summary"]["unique_clients"]),
        ("E4:F4", "E5:F6", "NO REGISTRADAS EN ALEGRA", report["summary"]["new_clients"]),
        ("G4:H4", "G5:H6", "CLIENTES RECURRENTES", report["summary"]["frequent_clients"]),
    ]
    for title_range, value_range, title, value in metrics:
        sheet.merge_cells(title_range)
        sheet.merge_cells(value_range)
        title_cell = sheet[title_range.split(":")[0]]
        value_cell = sheet[value_range.split(":")[0]]
        title_cell.value = title
        title_cell.font = Font(size=9, bold=True, color=white)
        title_cell.fill = PatternFill("solid", fgColor=blue)
        title_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        value_cell.value = int(value or 0)
        value_cell.font = Font(size=23, bold=True, color=navy)
        value_cell.fill = PatternFill("solid", fgColor=pale_blue)
        value_cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in sheet[title_range]:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=blue)
        for row in sheet[value_range]:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=pale_blue)
                cell.border = Border(left=border, right=border, top=border, bottom=border)
    sheet.row_dimensions[4].height = 28
    sheet.row_dimensions[5].height = 25
    sheet.row_dimensions[6].height = 20

    sheet.merge_cells("A9:C9")
    sheet["A9"] = "ACCESOS POR ORIGEN"
    sheet["A9"].font = Font(size=12, bold=True, color=white)
    sheet["A9"].fill = PatternFill("solid", fgColor=navy)
    for cell in sheet[9][:3]:
        cell.fill = PatternFill("solid", fgColor=navy)

    headers = ["Origen", "Accesos", "Participación"]
    for col, header in enumerate(headers, 1):
        cell = sheet.cell(row=10, column=col, value=header)
        cell.font = Font(bold=True, color=navy)
        cell.fill = PatternFill("solid", fgColor=light)
        cell.border = Border(bottom=border)
        cell.alignment = Alignment(horizontal="left" if col == 1 else "center")

    total_accesses = int(report["summary"]["total_accesses"] or 0)
    for row_num, data in enumerate(report["by_origin"], 11):
        label = (data.get("origen") or "Sin especificar").capitalize()
        count = int(data.get("accesses") or 0)
        sheet.cell(row_num, 1, label)
        sheet.cell(row_num, 2, count)
        share = count / total_accesses if total_accesses else 0
        sheet.cell(row_num, 3, share).number_format = "0.0%"
        for col in range(1, 4):
            cell = sheet.cell(row_num, col)
            cell.border = Border(bottom=border)
            cell.alignment = Alignment(horizontal="left" if col == 1 else "center")
            if row_num % 2:
                cell.fill = PatternFill("solid", fgColor="FAFBFD")

    if report["by_origin"]:
        chart = BarChart()
        chart.type = "bar"
        chart.style = 10
        chart.title = "Accesos por origen"
        chart.y_axis.title = "Origen"
        chart.x_axis.title = "Accesos"
        chart.height = 6.5
        chart.width = 12
        chart.legend = None
        chart.add_data(Reference(sheet, min_col=2, min_row=10, max_row=10 + len(report["by_origin"])), titles_from_data=True)
        chart.set_categories(Reference(sheet, min_col=1, min_row=11, max_row=10 + len(report["by_origin"])))
        chart.dataLabels = DataLabelList()
        chart.dataLabels.showVal = True
        sheet.add_chart(chart, "E9")

    sheet.freeze_panes = "A10"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 1
    sheet.page_setup.orientation = "landscape"
    sheet.print_area = "A1:H13"

    def add_client_sheet(
        title: str,
        heading: str,
        rows: list[dict[str, Any]],
        include_origins: bool = False,
    ) -> None:
        detail = workbook.create_sheet(title)
        detail.sheet_view.showGridLines = False
        columns = ["Nombre", "Celular", "Accesos", "Último acceso"]
        if include_origins:
            columns.append("Origen")
        detail.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(columns))
        title_cell = detail.cell(1, 1, heading)
        title_cell.font = Font(size=16, bold=True, color=white)
        title_cell.fill = PatternFill("solid", fgColor=navy)
        title_cell.alignment = Alignment(vertical="center")
        for col in range(1, len(columns) + 1):
            detail.cell(1, col).fill = PatternFill("solid", fgColor=navy)
        detail.row_dimensions[1].height = 32
        detail.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(columns))
        period_cell = detail.cell(
            2,
            1,
            f"Período: {start_date.strftime('%d/%m/%Y')} – {end_date.strftime('%d/%m/%Y')}",
        )
        period_cell.font = Font(size=10, color=gray, italic=True)

        for col, header in enumerate(columns, 1):
            cell = detail.cell(4, col, header)
            cell.font = Font(bold=True, color=white)
            cell.fill = PatternFill("solid", fgColor=blue)
            cell.alignment = Alignment(horizontal="left" if col == 1 else "center")
        for row_idx, client in enumerate(rows, 5):
            values = [
                client.get("name") or "Sin nombre",
                client.get("phone") or "No registrado",
                int(client.get("accesses") or 0),
                client["last_access"],
            ]
            if include_origins:
                values.append(client.get("origins") or "")
            for col, value in enumerate(values, 1):
                cell = detail.cell(row_idx, col, value)
                cell.border = Border(bottom=border)
                if row_idx % 2:
                    cell.fill = PatternFill("solid", fgColor="FAFBFD")
                cell.alignment = Alignment(
                    horizontal="left" if col in (1, 2, len(values)) else "center",
                    vertical="center",
                )
                if col == 4 and value:
                    cell.number_format = "dd/mm/yyyy hh:mm"
        if not rows:
            detail.merge_cells(start_row=5, start_column=1, end_row=5, end_column=len(columns))
            detail.cell(5, 1, "No hay registros para los filtros seleccionados.")
            detail.cell(5, 1).font = Font(italic=True, color=gray)
        widths_by_col = [34, 20, 14, 23, 18]
        for col, width in enumerate(widths_by_col[:len(columns)], 1):
            detail.column_dimensions[get_column_letter(col)].width = width
        detail.auto_filter.ref = f"A4:{get_column_letter(len(columns))}{max(4, 4 + len(rows))}"
        detail.freeze_panes = "A5"
        detail.sheet_properties.pageSetUpPr.fitToPage = True
        detail.page_setup.fitToWidth = 1
        detail.page_setup.fitToHeight = 0
        detail.page_setup.orientation = "landscape"
        detail.print_title_rows = "1:4"

    add_client_sheet(
        "Clientes frecuentes",
        "CLIENTES QUE CONSULTAN CON FRECUENCIA",
        report["frequent_clients"],
        include_origins=True,
    )
    add_client_sheet(
        "Nuevos en Alegra",
        "PERSONAS NO REGISTRADAS EN ALEGRA",
        report["new_clients"],
    )

    notes_row = 13
    sheet.merge_cells(start_row=notes_row, start_column=1, end_row=notes_row, end_column=8)
    note = sheet.cell(
        notes_row,
        1,
        "Fuente: MySQL · Solo accesos exitosos · No contiene documentos, correos, teléfonos ni direcciones IP.",
    )
    note.font = Font(size=9, color=gray, italic=True)
    note.alignment = Alignment(wrap_text=True)
    sheet.row_dimensions[notes_row].height = 30

    widths = {"A": 22, "B": 16, "C": 20, "D": 4, "E": 17, "F": 17, "G": 17, "H": 17}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
