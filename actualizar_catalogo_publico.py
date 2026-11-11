"""
Actualización del catálogo público (clientes finales).
Se ejecuta 15 minutos después de actualizar_catalogo.py.

El script principal ya dejó data/items.json fresco — este script solo
regenera la caché desde las tablas actuales. Sin llamadas a la API,
sin regenerar Sheets, sin descargar imágenes.

Horarios (America/Bogota):  03:15  10:15  13:15  16:15

Uso manual:
    py actualizar_catalogo_publico.py
"""

import json
import os
import sys
from datetime import datetime

# Asegurar que estamos en el directorio correcto
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log("═" * 50)
    log("ACTUALIZACIÓN CATÁLOGO PÚBLICO")
    log("═" * 50)

    items_path = "data/items.json"

    # Verificar que items.json existe
    if not os.path.exists(items_path):
        log(f"ERROR: no existe {items_path}.")
        log("  Ejecuta primero actualizar_catalogo.py")
        sys.exit(1)

    # Avisar si items.json es muy antiguo (más de 2 horas)
    edad_min = (datetime.now().timestamp() - os.path.getmtime(items_path)) / 60
    if edad_min > 120:
        log(f"⚠ items.json tiene {edad_min:.0f} min de antigüedad (esperado < 120). Continuando...")
    else:
        log(f"  items.json tiene {edad_min:.1f} min de antigüedad — OK")

    # Cargar items
    with open(items_path, encoding="utf-8") as f:
        items = json.load(f)
    log(f"  → {len(items)} items cargados")

    # Regenerar caché web (JSON + HTML) para que la app sirva datos frescos
    log("Regenerando caché web...")
    try:
        from src.catalog_cache import rebuild_all
        rebuild_all()
        log("  → caché regenerado")
    except Exception as e:
        log(f"⚠ No se pudo regenerar el caché: {e}")

    log("═" * 50)
    log("✓ CATÁLOGO PÚBLICO ACTUALIZADO")
    log("═" * 50)


if __name__ == "__main__":
    main()
