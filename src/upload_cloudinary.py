"""
Sube imágenes de data/images/ a Cloudinary y guarda las URLs públicas.

Características:
- Resume: si ya se subió una imagen (existe en images_urls.json con URL de Cloudinary), la salta
- Progreso: muestra barra de progreso y conteo
- Concurrencia: sube en paralelo (configurable) para acelerar
- Reintentos: 3 intentos por imagen antes de reportar error

Uso:
    python -m src.upload_cloudinary
    python src/upload_cloudinary.py
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

# Cargar .env
load_dotenv()

# Configuración Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

# Rutas
IMAGES_DIR = Path("data/images")
URLS_FILE = Path("data/images_urls.json")

# Configuración de upload
MAX_WORKERS = 10  # uploads paralelos
MAX_RETRIES = 3
FOLDER = "catalogo_alianxa"  # carpeta en Cloudinary


def load_existing_urls() -> dict:
    """Carga URLs existentes (para resume)."""
    if URLS_FILE.exists():
        try:
            with open(URLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_urls(urls: dict):
    """Guarda URLs de forma atómica."""
    URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = URLS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(urls, f, ensure_ascii=False, indent=2)
    tmp.replace(URLS_FILE)


def is_cloudinary_url(url: str) -> bool:
    """Verifica si una URL ya es de Cloudinary."""
    return "cloudinary" in url or "res.cloudinary.com" in url


def _load_changed_ids() -> set:
    """Carga los IDs que cambiaron de imagen en Alegra y necesitan overwrite."""
    changed_file = Path("data/.images_changed.json")
    if changed_file.exists():
        try:
            with open(changed_file, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _clear_changed_ids() -> None:
    """Limpia el archivo de IDs cambiados tras procesarlos."""
    changed_file = Path("data/.images_changed.json")
    changed_file.unlink(missing_ok=True)


def upload_single_image(image_path: Path, item_id: str, overwrite: bool = False) -> tuple:
    """
    Sube una imagen a Cloudinary.
    overwrite=True fuerza la re-subida aunque ya exista (para imágenes actualizadas en Alegra).
    Retorna (item_id, url) en éxito o (item_id, None) en fallo.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = cloudinary.uploader.upload(
                str(image_path),
                public_id=f"{FOLDER}/{item_id}",
                overwrite=overwrite,
                invalidate=overwrite,   # invalida CDN cache si se sobreescribe
                resource_type="image",
                transformation=[
                    {"width": 400, "height": 400, "crop": "limit", "quality": "auto:good"}
                ],
            )
            url = result.get("secure_url", "")
            if url:
                cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
                public_id = f"{FOLDER}/{item_id}"
                thumb_url = f"https://res.cloudinary.com/{cloud_name}/image/upload/w_200,h_200,c_limit,q_auto/{public_id}.jpg"
                return (item_id, thumb_url)
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"\n  ERROR {item_id}: {e}")
                return (item_id, None)
            time.sleep(1 * attempt)

    return (item_id, None)


def main():
    """Ejecuta el upload masivo a Cloudinary."""
    print(f"\n{'='*60}")
    print(f" UPLOAD DE IMÁGENES A CLOUDINARY")
    print(f"{'='*60}")

    # Verificar configuración
    if not os.getenv("CLOUDINARY_CLOUD_NAME"):
        print("ERROR: CLOUDINARY_CLOUD_NAME no configurado en .env")
        sys.exit(1)

    # Listar imágenes disponibles
    if not IMAGES_DIR.exists():
        print(f"ERROR: Directorio {IMAGES_DIR} no existe")
        sys.exit(1)

    all_images = sorted(IMAGES_DIR.glob("*.jpg"))
    total = len(all_images)
    print(f"  Imágenes encontradas: {total}")

    # Cargar URLs existentes para resume
    urls = load_existing_urls()

    # Cargar IDs que cambiaron imagen en Alegra → necesitan overwrite
    changed_ids = _load_changed_ids()
    if changed_ids:
        print(f"  IDs con imagen actualizada en Alegra: {len(changed_ids)} (se re-subirán)")

    # Filtrar: subir las que NO tienen URL de Cloudinary O cuya imagen cambió
    to_upload = []
    for img in all_images:
        item_id = img.stem
        existing_url = urls.get(item_id, "")
        needs_overwrite = item_id in changed_ids
        if not is_cloudinary_url(existing_url) or needs_overwrite:
            to_upload.append((img, item_id, needs_overwrite))

    already_done = total - len(to_upload)
    print(f"  Ya subidas (Cloudinary): {already_done}")
    print(f"  Pendientes: {len(to_upload)}")

    if not to_upload:
        print("\n  Todas las imágenes ya están en Cloudinary.")
        print(f"{'='*60}\n")
        _clear_changed_ids()
        return

    print(f"\n  Subiendo {len(to_upload)} imágenes ({MAX_WORKERS} en paralelo)...")
    print(f"  Carpeta Cloudinary: {FOLDER}/")
    print()

    # Upload paralelo
    uploaded = 0
    errors = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(upload_single_image, img, item_id, overwrite): item_id
            for img, item_id, overwrite in to_upload
        }

        for future in as_completed(futures):
            item_id, url = future.result()
            if url:
                urls[item_id] = url
                uploaded += 1
            else:
                errors += 1

            done = uploaded + errors
            pct = (done / len(to_upload)) * 100
            elapsed = time.time() - start_time
            rate = done / elapsed if elapsed > 0 else 0

            # Progreso en una línea
            sys.stdout.write(
                f"\r  [{done}/{len(to_upload)}] {pct:.1f}% | "
                f"OK: {uploaded} | Err: {errors} | "
                f"{rate:.1f} img/s"
            )
            sys.stdout.flush()

            # Guardar cada 50 uploads para no perder progreso
            if done % 50 == 0:
                save_urls(urls)

    # Guardar final
    save_urls(urls)

    elapsed = time.time() - start_time
    print(f"\n\n{'='*60}")
    print(f" UPLOAD COMPLETADO")
    print(f"{'='*60}")
    print(f"  Subidas exitosamente: {uploaded}")
    print(f"  Errores: {errors}")
    print(f"  Tiempo total: {elapsed:.1f}s")
    print(f"  URLs guardadas en: {URLS_FILE}")
    print(f"{'='*60}\n")

    if errors > 0:
        # Listar IDs con error
        failed = [item_id for img, item_id, ow in to_upload if not is_cloudinary_url(urls.get(item_id, ""))]
        if failed:
            print(f"  IDs con error ({len(failed)}): {', '.join(failed[:20])}{'...' if len(failed) > 20 else ''}")
            print("  Ejecuta de nuevo para reintentar.\n")
    else:
        # Todo OK — limpiar lista de IDs cambiados
        _clear_changed_ids()


if __name__ == "__main__":
    main()
