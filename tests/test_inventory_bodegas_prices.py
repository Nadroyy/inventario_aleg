import unittest
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import openpyxl
import excel_ai.inventario_bodegas as inventario_bodegas
from excel_ai.inventario_bodegas import _parse_prices


class InventoryBodegaPriceTest(unittest.TestCase):
    def test_active_iva_is_added_to_instalador_and_cliente(self):
        item = {
            "price": [
                {"name": "INSTALADOR", "price": 30252.1},
                {"name": "CLIENTE", "price": 38403.4},
                {"name": "ALIA2", "price": 35000},
            ],
            "tax": [{"type": "IVA", "status": "active", "percentage": "19.00"}],
        }

        prices = _parse_prices(item)

        self.assertEqual(prices["instalador"], 36000)
        self.assertEqual(prices["cliente"], 45700)
        self.assertFalse(prices["dcto_volumen"])

    def test_missing_or_inactive_iva_does_not_change_prices(self):
        for taxes in ([], [{"type": "IVA", "status": "inactive", "percentage": "19"}]):
            with self.subTest(taxes=taxes):
                item = {
                    "price": [
                        {"name": "INSTALADOR", "price": 30252.1},
                        {"name": "CLIENTE", "price": 38403.4},
                    ],
                    "tax": taxes,
                }

                prices = _parse_prices(item)

                self.assertEqual(prices["instalador"], 30252.1)
                self.assertEqual(prices["cliente"], 38403.4)

    def test_active_zero_percent_iva_does_not_change_prices(self):
        item = {
            "price": [{"name": "INSTALADOR", "price": 100}],
            "tax": [{"type": "IVA", "status": "active", "percentage": "0"}],
        }

        self.assertEqual(_parse_prices(item)["instalador"], 100)

    def test_generated_workbook_places_total_real_first_and_link_last(self):
        item = {
            "reference": "TEST-TOTAL",
            "name": "Producto de prueba",
            "itemCategory": {"name": "Pruebas"},
            "inventory": {
                "warehouses": [
                    {"name": "Principal", "availableQuantity": 5, "remissionedQuantity": 2},
                    {"name": "Caobos", "availableQuantity": 4, "remissionedQuantity": 1},
                    {"name": "Bulevar", "availableQuantity": 4, "remissionedQuantity": 0},
                ]
            },
            "price": [
                {"name": "INSTALADOR", "price": 100},
                {"name": "CLIENTE", "price": 200},
            ],
            "tax": [{"type": "IVA", "status": "active", "percentage": "19"}],
            "customFields": [{"name": "LINK", "value": "https://example.test/product"}],
        }

        with TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "inventario.xlsx"
            with patch.object(inventario_bodegas, "OUTPUT_FILE", output_file):
                with redirect_stdout(StringIO()):
                    inventario_bodegas.generate_excel([item])

            workbook = openpyxl.load_workbook(output_file, data_only=True)
            sheet = workbook["INVENTARIO"]
            self.assertEqual(sheet["A1"].value, "REFERENCIA")
            self.assertEqual(sheet["C1"].value, "TOTAL REAL")
            self.assertEqual(sheet["C4"].value, 10)
            self.assertEqual(sheet["D1"].value, "STOCK")
            self.assertEqual(sheet["F1"].value, "REAL")
            self.assertEqual(sheet["M1"].value, "INSTALADOR + IVA")
            self.assertEqual(sheet["M4"].value, 119)
            self.assertEqual(sheet["N4"].value, 238)
            self.assertEqual(sheet["O1"].value, "DCTO VOL")
            self.assertEqual(sheet["P1"].value, "LINK")
            self.assertEqual(sheet["P4"].value, "https://example.test/product")

            con_stock = workbook["CON STOCK"]
            self.assertEqual(con_stock["A1"].value, "TOTAL REAL")
            self.assertEqual(con_stock["D1"].value, "INSTALADOR + IVA")
            self.assertEqual(con_stock["D4"].value, 119)
            self.assertEqual(con_stock["E4"].value, 238)
            self.assertEqual(con_stock["F1"].value, "DCTO VOL")
            self.assertEqual(con_stock["G1"].value, "LINK")
            self.assertEqual(con_stock["G4"].value, "https://example.test/product")
            self.assertEqual(con_stock["A3"].value, "PRUEBAS")
            self.assertEqual(con_stock["A3"].fill.fgColor.rgb, "002E75B6")
            self.assertEqual(con_stock["A3"].font.color.rgb, "00FFFFFF")
            workbook.close()

    def test_failed_page_aborts_without_writing_partial_cache(self):
        page_items = [
            {
                "type": "product",
                "reference": f"TEST-{index}",
                "status": "active",
                "settingsOnShop": {"hide": False},
            }
            for index in range(30)
        ]

        with TemporaryDirectory() as temp_dir:
            cache_file = Path(temp_dir) / "data_cache.json"
            with (
                patch.object(inventario_bodegas, "DATA_CACHE_FILE", cache_file),
                patch.object(inventario_bodegas, "_load_checkpoint", return_value=None),
                patch.object(inventario_bodegas, "_save_checkpoint"),
                patch.object(inventario_bodegas, "_fetch_page", side_effect=[page_items, None]),
                patch.object(inventario_bodegas.time, "sleep"),
                redirect_stdout(StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "página 2"):
                    inventario_bodegas.fetch_all_inventory()

            self.assertFalse(cache_file.exists())

    def test_successful_download_marks_cache_complete(self):
        item = {
            "type": "product",
            "reference": "TEST-1",
            "status": "active",
            "settingsOnShop": {"hide": False},
        }

        with TemporaryDirectory() as temp_dir:
            cache_file = Path(temp_dir) / "data_cache.json"
            with (
                patch.object(inventario_bodegas, "DATA_CACHE_FILE", cache_file),
                patch.object(inventario_bodegas, "_load_checkpoint", return_value=None),
                patch.object(inventario_bodegas, "_save_checkpoint"),
                patch.object(inventario_bodegas, "_fetch_page", return_value=[item]),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(inventario_bodegas.fetch_all_inventory(), [item])

            cache = __import__("json").loads(cache_file.read_text(encoding="utf-8"))
            self.assertIs(cache["complete"], True)

    def test_json_export_includes_hidden_store_products(self):
        active_items = [
            {
                "id": str(index),
                "type": "product",
                "status": "active",
                "reference": f"REF-{index}",
                "settingsOnShop": {"hide": False},
            }
            for index in range(500)
        ]
        invalid_items = [
            {**active_items[0], "type": "service", "reference": "SERV-1"},
            {**active_items[0], "reference": ""},
            {**active_items[0], "reference": "HIDDEN-1", "settingsOnShop": {"hide": True}},
            {**active_items[0], "reference": "INACTIVE-1", "status": "inactive"},
        ]

        with TemporaryDirectory() as temp_dir:
            items_path = Path(temp_dir) / "items.json"
            output_path = Path(temp_dir) / "inventory.xlsx"
            items_path.write_text(json.dumps(active_items + invalid_items), encoding="utf-8")
            captured = {}

            def build_workbook(items):
                captured["items"] = items
                return openpyxl.Workbook(), len(items), 1

            with (
                patch.object(inventario_bodegas, "OUTPUT_FILE", output_path),
                patch.object(inventario_bodegas, "_build_workbook", side_effect=build_workbook),
                patch("excel_ai.cambios_bodegas.aplicar_cambios"),
                redirect_stdout(StringIO()),
            ):
                inventario_bodegas.generate_excel_from_json(str(items_path))

            self.assertEqual(len(captured["items"]), 501)
            self.assertTrue(all(item["type"] == "product" for item in captured["items"]))
            self.assertTrue(all(item["status"] == "active" for item in captured["items"]))
            self.assertTrue(all(item["reference"] for item in captured["items"]))
            self.assertIn("HIDDEN-1", {item["reference"] for item in captured["items"]})
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()