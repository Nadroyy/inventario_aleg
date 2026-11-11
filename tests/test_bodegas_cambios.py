"""
Tests unitarios para excel_ai/cambios_bodegas.py.

Cubre la lógica pura de diffs (_compute_cambios, _construir_snapshot, _stock_por_bodega)
sin acceso a red ni Google Drive.
"""

import unittest
from openpyxl import Workbook
from excel_ai.cambios_bodegas import (
    _agregar_hoja_cambios_al_workbook,
    _agrupar_historial,
    _compute_cambios,
    _construir_snapshot,
    _color_para_cambio,
    _stock_por_bodega,
)


def _make_item(ref, nombre="Producto", principal=0, caobos=0, bulevar=0,
               rem_principal=0, rem_caobos=0, rem_bulevar=0,
               status="active", hide=False):
    """Helper que construye un item con la estructura mínima de Alegra."""
    return {
        "reference": ref,
        "name": nombre,
        "type": "product",
        "status": status,
        "settingsOnShop": {"hide": hide},
        "inventory": {
            "warehouses": [
                {"name": "Principal", "availableQuantity": principal, "remissionedQuantity": rem_principal},
                {"name": "Caobos",    "availableQuantity": caobos,    "remissionedQuantity": rem_caobos},
                {"name": "Bulevar",   "availableQuantity": bulevar,   "remissionedQuantity": rem_bulevar},
            ]
        },
    }


class TestStockPorBodega(unittest.TestCase):
    def test_reales_calculados_correctamente(self):
        item = _make_item("REF-1", principal=10, caobos=5, bulevar=3,
                          rem_principal=2, rem_caobos=1, rem_bulevar=0)
        stock = _stock_por_bodega(item)
        self.assertEqual(stock["stock_principal"], 8)   # 10 - 2
        self.assertEqual(stock["stock_caobos"],    4)   # 5  - 1
        self.assertEqual(stock["stock_bulevar"],   3)   # 3  - 0
        self.assertEqual(stock["stock_total"],    15)   # 8 + 4 + 3

    def test_sin_bodegas_retorna_cero(self):
        item = {"inventory": {"warehouses": []}}
        stock = _stock_por_bodega(item)
        self.assertEqual(stock["stock_principal"], 0)
        self.assertEqual(stock["stock_total"],     0)

    def test_bodega_faltante_se_trata_como_cero(self):
        item = {"inventory": {"warehouses": [
            {"name": "Principal", "availableQuantity": 5, "remissionedQuantity": 0},
        ]}}
        stock = _stock_por_bodega(item)
        self.assertEqual(stock["stock_principal"], 5)
        self.assertEqual(stock["stock_caobos"],    0)
        self.assertEqual(stock["stock_bulevar"],   0)
        self.assertEqual(stock["stock_total"],     5)


class TestConstruirSnapshot(unittest.TestCase):
    def test_snapshot_incluye_solo_activos_y_visibles(self):
        items = [
            _make_item("A", "Activo visible", principal=3),
            _make_item("B", "Activo oculto",  principal=5, hide=True),
            _make_item("C", "Inactivo",        principal=2, status="inactive"),
        ]
        snap = _construir_snapshot(items)
        self.assertIn("A", snap)
        self.assertNotIn("B", snap)
        self.assertNotIn("C", snap)

    def test_snapshot_contiene_campos_correctos(self):
        items = [_make_item("X", "Producto X", principal=4, caobos=2, bulevar=1,
                             rem_principal=1)]
        snap = _construir_snapshot(items)
        self.assertEqual(snap["X"]["nombre"],          "Producto X")
        self.assertEqual(snap["X"]["stock_principal"], 3)  # 4 - 1
        self.assertEqual(snap["X"]["stock_caobos"],    2)
        self.assertEqual(snap["X"]["stock_bulevar"],   1)
        self.assertEqual(snap["X"]["stock_total"],     6)

    def test_snapshot_excluye_no_productos_y_sin_referencia(self):
        servicio = _make_item("SERV-1", "Servicio")
        servicio["type"] = "service"
        sin_referencia = _make_item("", "Sin referencia")

        snap = _construir_snapshot([servicio, sin_referencia])

        self.assertEqual(snap, {})


class TestComputeCambios(unittest.TestCase):
    def test_primera_sincronizacion(self):
        items = [_make_item("R1", "Prod", principal=5)]
        nuevas_filas, nuevo_snap = _compute_cambios({}, items)

        self.assertEqual(len(nuevas_filas), 1)
        self.assertIn("Primera sincronización", nuevas_filas[0])
        self.assertIn("R1", nuevo_snap)

    def test_sin_cambios_retorna_lista_vacia(self):
        items = [_make_item("R1", "Prod", principal=5)]
        snap = _construir_snapshot(items)
        nuevas_filas, _ = _compute_cambios(snap, items)
        self.assertEqual(nuevas_filas, [])

    def test_cambio_en_stock_principal(self):
        items_antes = [_make_item("R1", "Prod", principal=5)]
        snap_antes  = _construir_snapshot(items_antes)

        items_ahora = [_make_item("R1", "Prod", principal=8)]
        nuevas_filas, _ = _compute_cambios(snap_antes, items_ahora)

        campos_cambiados = [f[3] for f in nuevas_filas]
        self.assertIn("stock_principal", campos_cambiados)
        self.assertEqual(nuevas_filas[0][3], "Actualización")

        fila = next(f for f in nuevas_filas if f[3] == "stock_principal")
        self.assertEqual(fila[4], "5")  # valor anterior
        self.assertEqual(fila[5], "8")  # valor nuevo
        self.assertEqual(fila[1], "R1")

    def test_producto_nuevo(self):
        items_antes = [_make_item("R1", "Prod A", principal=3)]
        snap_antes  = _construir_snapshot(items_antes)

        items_ahora = [
            _make_item("R1", "Prod A", principal=3),
            _make_item("R2", "Prod B", principal=2),
        ]
        nuevas_filas, _ = _compute_cambios(snap_antes, items_ahora)

        tipos = [f[3] for f in nuevas_filas]
        self.assertIn("Producto nuevo", tipos)

        fila = next(f for f in nuevas_filas if f[3] == "Producto nuevo")
        self.assertEqual(fila[1], "R2")

    def test_producto_eliminado(self):
        items_antes = [
            _make_item("R1", "Prod A", principal=3),
            _make_item("R2", "Prod B", principal=2),
        ]
        snap_antes  = _construir_snapshot(items_antes)

        items_ahora = [_make_item("R1", "Prod A", principal=3)]
        nuevas_filas, _ = _compute_cambios(snap_antes, items_ahora)

        tipos = [f[3] for f in nuevas_filas]
        self.assertIn("Producto eliminado", tipos)

        fila = next(f for f in nuevas_filas if f[3] == "Producto eliminado")
        self.assertEqual(fila[1], "R2")

    def test_stock_total_es_suma_de_reales(self):
        items = [_make_item("R1", "Prod", principal=4, caobos=3, bulevar=2,
                             rem_principal=1)]
        snap = _construir_snapshot(items)
        self.assertEqual(snap["R1"]["stock_total"], 8)  # 3 + 3 + 2



class TestHojaCambios(unittest.TestCase):
    def test_colores_indican_aumento_y_disminucion(self):
        verde = object()
        rojo = object()

        self.assertIs(
            _color_para_cambio(["", "R1", "", "stock_total", "8", "5"], verde, rojo),
            verde,
        )
        self.assertIs(
            _color_para_cambio(["", "R1", "", "stock_total", "5", "8"], verde, rojo),
            rojo,
        )
        self.assertIs(
            _color_para_cambio(["", "R1", "", "Producto eliminado", "", ""], verde, rojo),
            verde,
        )

    def test_separador_de_actualizacion_se_destaca_en_el_workbook(self):
        workbook = Workbook()
        _agregar_hoja_cambios_al_workbook(workbook, [
            ["Fecha", "Referencia", "Nombre", "Campo Cambiado", "Valor Anterior", "Valor Nuevo"],
            ["21/10/2026 14:55", "", "", "Actualización", "", ""],
            ["21/10/2026 14:55", "REF-1", "Producto", "stock_total", "2", "3"],
        ])

        sheet = workbook["CAMBIOS"]
        self.assertEqual(sheet["D2"].value, "Actualización")
        self.assertEqual(sheet["A2"].fill.fgColor.rgb, "00D1E3F4")
        self.assertTrue(sheet["D2"].font.bold)
        self.assertEqual(sheet.sheet_state, "hidden")

    def test_historial_legacy_se_agrupa_por_fecha(self):
        filas = [
            ["Fecha", "Referencia", "Nombre", "Campo Cambiado", "Valor Anterior", "Valor Nuevo"],
            ["22/10/2026 10:00", "R1", "Prod 1", "stock_total", "1", "2"],
            ["22/10/2026 10:00", "R2", "Prod 2", "stock_total", "3", "4"],
            ["23/10/2026 10:17", "R3", "Prod 3", "stock_total", "5", "6"],
        ]

        agrupadas = _agrupar_historial(filas)
        separadores = [fila for fila in agrupadas if fila[3] == "Actualización"]

        self.assertEqual(len(separadores), 2)
        self.assertEqual(separadores[0][0], "22/10/2026 10:00")
        self.assertEqual(separadores[1][0], "23/10/2026 10:17")
        self.assertEqual(len(agrupadas), 6)


if __name__ == "__main__":
    unittest.main()
