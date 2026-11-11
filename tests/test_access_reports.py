import unittest
from datetime import date, datetime
from io import BytesIO

from openpyxl import load_workbook

from src.access_reports import build_access_filters, build_access_report_xlsx


class AccessReportFilterTests(unittest.TestCase):
    def test_date_range_is_inclusive_and_parameterized(self):
        sql, params = build_access_filters(date(2026, 11, 1), date(2026, 11, 3))

        self.assertIn("fecha >= %s", sql)
        self.assertIn("fecha < %s", sql)
        self.assertEqual(
            params,
            [datetime(2026, 11, 1), datetime(2026, 11, 4)],
        )

    def test_origin_filter_is_parameterized(self):
        sql, params = build_access_filters(
            date(2026, 11, 1), date(2026, 11, 3), "excel"
        )

        self.assertIn("origen = %s", sql)
        self.assertEqual(params[-1], "excel")

    def test_excel_report_contains_formatted_summary_and_origin_table(self):
        content = build_access_report_xlsx(
            {
                "summary": {
                    "total_accesses": 12,
                    "unique_clients": 8,
                    "new_clients": 3,
                    "frequent_clients": 2,
                },
                "by_origin": [
                    {"origen": "excel", "accesses": 7},
                    {"origen": "publico", "accesses": 5},
                ],
                "frequent_clients": [
                    {
                        "name": "Cliente de prueba",
                        "phone": "PHONE_TEST",
                        "accesses": 3,
                        "last_access": datetime(2026, 11, 10, 12, 0),
                        "origins": "excel",
                    }
                ],
                "new_clients": [],
            },
            date(2026, 11, 1),
            date(2026, 11, 11),
        )

        workbook = load_workbook(BytesIO(content))
        sheet = workbook["Resumen de accesos"]
        self.assertEqual(sheet["A1"].value, "REPORTE DE ACCESOS AL CATÁLOGO")
        self.assertEqual(sheet["A5"].value, 12)
        self.assertEqual(sheet["B11"].value, 7)
        self.assertEqual(len(sheet._charts), 1)
        self.assertEqual(
            workbook["Clientes frecuentes"]["A1"].value,
            "CLIENTES QUE CONSULTAN CON FRECUENCIA",
        )
        self.assertEqual(workbook["Clientes frecuentes"]["B5"].value, "PHONE_TEST")
        self.assertEqual(
            workbook["Nuevos en Alegra"]["A5"].value,
            "No hay registros para los filtros seleccionados.",
        )


if __name__ == "__main__":
    unittest.main()
