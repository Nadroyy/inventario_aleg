"""Regenera el Excel de inventario usando el caché local y lo sube a Drive.
No llama a la API de Alegra."""
import json, sys, os
from pathlib import Path
from datetime import date

os.chdir(os.path.dirname(os.path.abspath(__file__)))
try: sys.stdout.reconfigure(encoding='utf-8')
except: pass

cache_file = Path('excel_ai/data_cache.json')

if not cache_file.exists():
    print("ERROR: no hay caché en excel_ai/data_cache.json")
    sys.exit(1)

with open(cache_file, encoding='utf-8') as f:
    cache = json.load(f)

items = cache.get('items', [])
print(f"Caché: {cache.get('date')} | {len(items)} items")

if len(items) < 500:
    print(f"ERROR: caché tiene solo {len(items)} items — no es seguro generar el Excel")
    sys.exit(1)

# Limpiar checkpoint para evitar que interfiera
from excel_ai.inventario_bodegas import generate_excel, _clear_checkpoint
_clear_checkpoint()

print("Generando Excel...")
generate_excel(items)

print("Subiendo a Drive...")
from excel_ai.upload_drive import upload
upload()

print("Listo.")
