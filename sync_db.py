"""
sync_db.py — Sincroniza items de Alegra → MySQL (inventario_local)

Modos de uso:
  python sync_db.py            # Descarga items frescos de Alegra API y sincroniza
  python sync_db.py --local    # Usa data/items.json en disco (sin llamar a la API)
  python sync_db.py --help     # Muestra esta ayuda
"""

import sys
import json
import argparse
from datetime import datetime

# ---------------------------------------------------------------------------
# Parseo de argumentos
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(
    description="Sincroniza el inventario de Alegra a MySQL local."
)
parser.add_argument(
    "--local",
    action="store_true",
    help="Usar data/items.json en disco en vez de llamar a la API de Alegra.",
)
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Imports del proyecto (después de parsear para que --help funcione rápido)
# ---------------------------------------------------------------------------
from src.config import validate_config
from src.database import sync_items, sync_from_json

print("=" * 60)
print(" SYNC ALEGRA → MYSQL")
print(f" {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

# ---------------------------------------------------------------------------
# Modo --local: sincroniza desde items.json en disco
# ---------------------------------------------------------------------------
if args.local:
    print("\n[MODO LOCAL] Usando data/items.json (sin llamar a la API)\n")
    resumen = sync_from_json(
        items_path="data/items.json",
        images_path="data/images_urls.json",
    )

# ---------------------------------------------------------------------------
# Modo normal: descarga items frescos de Alegra y sincroniza
# ---------------------------------------------------------------------------
else:
    validate_config()

    from src.alegra_client import AlegraClient

    print("\n[1/3] Descargando items desde Alegra API...")
    cliente = AlegraClient()
    items = cliente.get_all_paginated(
        "items",
        params={"type": "product", "fields": "id,name,reference,description,status,itemCategory,price,inventory,tax"},
    )

    if not items:
        print("✗ No se obtuvieron items de Alegra. Verifica las credenciales.")
        sys.exit(1)

    print(f"  → {len(items)} items descargados")

    # Guardar copia en disco (actualiza el cache)
    print("\n[2/3] Guardando cache en data/items.json...")
    with open("data/items.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print("  → Cache actualizado")

    # Cargar URLs de imágenes
    import os
    images_urls = {}
    if os.path.exists("data/images_urls.json"):
        with open("data/images_urls.json", encoding="utf-8") as f:
            images_urls = json.load(f)
        print(f"  → {len(images_urls)} URLs de imágenes cargadas")

    print("\n[3/3] Sincronizando a MySQL...")
    resumen = sync_items(items, images_urls)

# ---------------------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------------------
print()
print("=" * 60)
print(" RESULTADO")
print("=" * 60)
print(f"  Total items procesados : {resumen['total']}")
print(f"  Insertados (nuevos)    : {resumen['insertados']}")
print(f"  Actualizados           : {resumen['actualizados']}")
print(f"  Errores                : {resumen['errores']}")
print("=" * 60)

if resumen["errores"] > 0:
    print("\n⚠ Hubo errores. Revisa la tabla sync_log para más detalles.")
else:
    print("\n✓ Sincronización completada sin errores.")

# ---------------------------------------------------------------------------
# Regenerar caché de catálogo (JSON en data/) para que la app web
# sirva los datos frescos sin hacer queries MySQL en cada visita.
# Se ejecuta siempre, incluso si hubo errores parciales, para que los
# productos que sí se sincronizaron queden reflejados en el catálogo.
# ---------------------------------------------------------------------------
print()
print("=" * 60)
print(" REGENERANDO CACHÉ DE CATÁLOGO")
print("=" * 60)
try:
    from src.catalog_cache import rebuild_all
    rebuild_all()
except Exception as e:
    print(f"⚠ No se pudo regenerar el caché: {e}")
    print("  El catálogo web usará los datos anteriores hasta el próximo sync.")

if resumen["errores"] > 0:
    sys.exit(1)
