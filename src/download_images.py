"""Descarga las imágenes de los items desde la CDN de Alegra.

Freshness: guarda en data/images_meta.json la URL de origen de cada imagen.
Si Alegra cambia la imagen de un producto, la próxima sincronización detecta
que la URL cambió y re-descarga + re-sube a Cloudinary con overwrite=True.
"""

import json
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

IMAGES_DIR = Path("data/images")
META_FILE  = Path("data/images_meta.json")   # { "item_id": "url_alegra" }


def load_items(filename="data/items.json"):
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_meta() -> dict:
    """Carga el mapa item_id → url_alegra."""
    if META_FILE.exists():
        try:
            with open(META_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_meta(meta: dict) -> None:
    """Guarda el mapa de forma atómica."""
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = META_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    tmp.replace(META_FILE)


def download_image(item_id, url, output_dir="data/images"):
    """Descarga una imagen y la guarda con el ID del item como nombre."""
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.content
            # Validar que el archivo no esté vacío o sea demasiado pequeño
            if len(data) < 100:
                return item_id, False, f"Contenido sospechoso ({len(data)} bytes)"

            content_type = response.headers.get("Content-Type", "")
            if "png" in content_type:
                ext = ".png"
            elif "gif" in content_type:
                ext = ".gif"
            elif "webp" in content_type:
                ext = ".webp"
            else:
                ext = ".jpg"

            filepath = os.path.join(output_dir, f"{item_id}{ext}")
            with open(filepath, "wb") as f:
                f.write(data)
            return item_id, True, filepath
        else:
            return item_id, False, f"HTTP {response.status_code}"
    except Exception as e:
        return item_id, False, str(e)


def download_all_images(solo_con_stock=True):
    """Descarga imágenes de productos.

    Novedad (freshness): compara la URL de Alegra con la guardada en
    images_meta.json. Si cambió, re-descarga aunque el archivo local exista.
    Esto garantiza que los cambios de imagen en Alegra se propaguen.
    """
    items = [item for item in load_items() if item.get("type") == "product"]

    if solo_con_stock:
        items = [
            item for item in items
            if isinstance(item.get("inventory"), dict)
            and (item["inventory"].get("availableQuantity", 0) or 0) > 0
        ]

    # Recopilar imágenes a descargar (id, url_actual_en_alegra)
    to_download = []
    for item in items:
        images = item.get("images", [])
        if images and isinstance(images, list):
            img = None
            for i in images:
                if isinstance(i, dict) and i.get("favorite"):
                    img = i
                    break
            if not img:
                img = images[0] if isinstance(images[0], dict) else None

            if img and "url" in img:
                to_download.append((str(item["id"]), img["url"]))

    print(f"Imágenes a descargar: {len(to_download)}")

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Cargar meta de URLs anteriores
    meta = _load_meta()

    # Determinar qué hay que (re)descargar:
    #   1. El archivo local no existe
    #   2. La URL en Alegra cambió respecto a la guardada en meta
    existing_files = {Path(f).stem for f in os.listdir(IMAGES_DIR)}

    to_download_new   = []   # nunca descargada
    to_download_fresh = []   # ya existe pero la URL cambió en Alegra

    for item_id, url in to_download:
        local_exists = item_id in existing_files
        url_changed  = meta.get(item_id) != url

        if not local_exists:
            to_download_new.append((item_id, url))
        elif url_changed:
            to_download_fresh.append((item_id, url))

    skipped = len(to_download) - len(to_download_new) - len(to_download_fresh)
    print(f"Ya descargadas (sin cambios): {skipped}")
    print(f"Nuevas:                       {len(to_download_new)}")
    print(f"Actualizadas en Alegra:       {len(to_download_fresh)}")

    all_pending = to_download_new + to_download_fresh
    if not all_pending:
        print("Todas las imágenes ya están actualizadas.")
        return

    print(f"Pendientes total:             {len(all_pending)}")

    # Descargar en paralelo (5 hilos)
    downloaded = 0
    failed     = 0
    # IDs cuya URL cambió → necesitan re-subida a Cloudinary con overwrite
    changed_ids: list[str] = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(download_image, item_id, url, str(IMAGES_DIR)): (item_id, url)
            for item_id, url in all_pending
        }

        for future in as_completed(futures):
            item_id, url = futures[future]
            item_id_r, success, _ = future.result()
            if success:
                downloaded += 1
                meta[item_id] = url   # actualizar URL guardada
                # Si era una actualización (no nueva), marcar para overwrite en Cloudinary
                if (item_id, url) in [(i, u) for i, u in to_download_fresh]:
                    changed_ids.append(item_id)
            else:
                failed += 1

            total_done = downloaded + failed
            if total_done % 50 == 0:
                print(f"  Progreso: {total_done}/{len(all_pending)} "
                      f"(✓ {downloaded} | ✗ {failed})")
                _save_meta(meta)   # guardar progreso parcial

    _save_meta(meta)

    print(f"\n{'═'*50}")
    print(f"  Descarga completada")
    print(f"  ✓ Exitosas: {downloaded}")
    print(f"  ✗ Fallidas: {failed}")
    print(f"  🔄 Actualizadas (URL cambió): {len(to_download_fresh)}")
    print(f"  Directorio: {IMAGES_DIR}/")
    print(f"{'═'*50}")

    # Escribir IDs que necesitan overwrite en Cloudinary
    # upload_cloudinary.py los leerá para forzar la re-subida
    if changed_ids:
        changed_file = Path("data/.images_changed.json")
        existing_changed = []
        if changed_file.exists():
            try:
                with open(changed_file, encoding="utf-8") as f:
                    existing_changed = json.load(f)
            except Exception:
                pass
        merged = list(set(existing_changed + changed_ids))
        tmp = changed_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f)
        tmp.replace(changed_file)
        print(f"  → {len(changed_ids)} IDs marcados para re-subida a Cloudinary")


if __name__ == "__main__":
    import sys
    solo_stock = "--all" not in sys.argv
    print("Descargando imágenes de items" + (" con stock" if solo_stock else " (todos)") + "...")
    download_all_images(solo_con_stock=solo_stock)
