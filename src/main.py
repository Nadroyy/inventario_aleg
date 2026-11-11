"""
MVP - Extracción de Items desde Alegra API
==========================================
Este script extrae todos los items activos de la cuenta de Alegra
y los guarda en un archivo JSON.

Uso:
    python -m src.main
"""

import os
from src.config import validate_config
from src.items import get_all_items, print_items_summary, save_items_to_json


def main():
    """Punto de entrada principal del MVP."""
    print("=" * 60)
    print(" ALEGRA - Extractor de Items (MVP)")
    print("=" * 60)
    print()

    # Validar credenciales
    validate_config()

    # Crear carpeta data si no existe
    os.makedirs("data", exist_ok=True)

    # Paso 1: Obtener todos los items activos
    items = get_all_items(status="active")

    if not items:
        print("\nNo se obtuvieron items.")
        print("Posibles causas:")
        print("  - Token API incorrecto (no es la contraseña de login)")
        print("  - No hay items activos en la cuenta")
        print("  - Error de conexión")
        return

    # Paso 2: Mostrar resumen
    print_items_summary(items)

    # Paso 3: Guardar en JSON
    save_items_to_json(items, "data/items.json")

    print("\n✓ Proceso completado exitosamente.")
    print(f"  Items extraídos: {len(items)}")
    print(f"  Archivo: data/items.json")


if __name__ == "__main__":
    main()
