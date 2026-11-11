"""Módulo para obtener y gestionar items de Alegra."""

import json
import os
from src.alegra_client import AlegraClient


def get_all_items(status="active", inventariable=None):
    """
    Extrae TODOS los items de Alegra, paginando automáticamente.
    
    Args:
        status: "active", "inactive" o None para todos
        inventariable: True para solo productos con inventario, None para todos
    
    Returns:
        Lista de items (dicts)
    """
    client = AlegraClient()
    # Pedimos campos extra:
    #  - remissionedQuantity: para la disponibilidad real (available - remissioned)
    #  - settingsOnShop: trae {"hide": true/false}; el catálogo solo muestra hide=false
    params = {"fields": "remissionedQuantity,settingsOnShop"}

    if status is not None:
        params["status"] = status

    if inventariable is not None:
        params["inventariable"] = str(inventariable).lower()

    label = status if status else "todos"
    print(f"Obteniendo items (status={label})...")
    items = client.get_all_paginated(
        "items",
        params=params,
        fail_on_first_page=True,
    )
    print(f"Total items obtenidos: {len(items)}")

    return items


def get_all_items_with_inactive():
    """
    Descarga activos e inactivos y los combina en una sola lista.
    Cada item lleva su campo 'status' original intacto.
    
    Returns:
        Lista combinada de items activos e inactivos
    """
    active = get_all_items(status="active")
    inactive = get_all_items(status="inactive")

    # Combinar evitando duplicados por ID
    seen = set()
    combined = []
    for item in active + inactive:
        item_id = item.get("id")
        if item_id not in seen:
            seen.add(item_id)
            combined.append(item)

    print(f"Total combinado (activos + inactivos): {len(combined)}")
    return combined


def print_items_summary(items):
    """Imprime un resumen de los items obtenidos en tabla."""
    print(f"\n{'='*90}")
    print(f" ITEMS EXTRAÍDOS: {len(items)}")
    print(f"{'='*90}\n")

    print(f"{'ID':<8} {'Nombre':<45} {'Referencia':<20} {'Tipo':<10} {'Stock':<10}")
    print("-" * 93)

    for item in items:
        item_id = item.get("id", "N/A")
        name = str(item.get("name", "Sin nombre"))[:43]
        reference = str(item.get("reference", "-"))[:18] if item.get("reference") else "-"
        item_type = item.get("type", "N/A")

        # El stock puede estar en inventory.availableQuantity
        inventory = item.get("inventory", {})
        if isinstance(inventory, dict):
            stock = inventory.get("availableQuantity", "-")
        else:
            stock = "-"

        print(f"{item_id:<8} {name:<45} {reference:<20} {item_type:<10} {stock:<10}")


def save_items_to_json(items, filename="data/items.json"):
    """Guarda los items en un archivo JSON de forma atómica (evita corrupción)."""
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filename)  # atómico en el mismo filesystem
    print(f"\nItems guardados en: {filename}")
