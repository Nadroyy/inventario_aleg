import os
import unittest

import mysql.connector

from src.database import sync_items


class DatabaseSyncActualSchemaTest(unittest.TestCase):
    def test_sync_items_uses_real_product_and_stock_tables(self):
        item_id = "SYNC_TEST_999999"
        warehouse_id = "WH_TEST_999999"

        conn = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "inventario_local"),
            charset="utf8mb4",
            autocommit=True,
        )
        cur = conn.cursor()
        cur.execute("DELETE FROM stock WHERE id_product = %s", (item_id,))
        cur.execute("DELETE FROM productos WHERE id_product = %s", (item_id,))
        cur.execute("DELETE FROM warehouses WHERE id_warehouse = %s", (warehouse_id,))
        conn.close()

        item = {
            "id": item_id,
            "name": "Producto de prueba",
            "reference": "REF-TEST-999",
            "description": "Producto insertado por prueba automatizada",
            "status": "active",
            "type": "product",
            "itemCategory": {"id": 99, "name": "Pruebas"},
            "settingsOnShop": {"hide": False},
            "price": [
                {"idPriceList": "1", "name": "ALIADOS", "price": "150000", "currency": {"code": "COP"}, "main": False},
                {"idPriceList": "2", "name": "INSTALADOR", "price": "180000", "currency": {"code": "COP"}, "main": False},
                {"idPriceList": "3", "name": "CLIENTE", "price": "210000", "currency": {"code": "COP"}, "main": True},
            ],
            "inventory": {
                "availableQuantity": 7,
                "warehouses": [
                    {
                        "id": warehouse_id,
                        "name": "Bodega prueba",
                        "isDefault": True,
                        "status": "active",
                        "availableQuantity": 7,
                        "minQuantity": 0,
                        "maxQuantity": 20,
                    }
                ],
            },
            "tax": [{"type": "IVA", "status": "active", "percentage": 19}],
        }

        resumen = sync_items([item], {item_id: "https://example.com/test.jpg"})

        self.assertEqual(resumen["total"], 1)
        self.assertEqual(resumen["errores"], 0)

        conn = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "inventario_local"),
            charset="utf8mb4",
            autocommit=True,
        )
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM productos WHERE id_product = %s", (item_id,))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT disponible FROM stock WHERE id_product = %s AND id_warehouse = %s", (item_id, warehouse_id))
            self.assertEqual(cur.fetchone()[0], 7)
        finally:
            cur.execute("DELETE FROM stock WHERE id_product = %s", (item_id,))
            cur.execute("DELETE FROM productos WHERE id_product = %s", (item_id,))
            cur.execute("DELETE FROM warehouses WHERE id_warehouse = %s", (warehouse_id,))
            conn.close()


if __name__ == "__main__":
    unittest.main()
