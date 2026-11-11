"""
generar_categorias.py
Asigna categorías automáticamente a los productos que no tienen
usando la API de Gemini. Solo usa categorías que ya existen en el sistema.
Procesa en lotes de 50 y actualiza la BD.

Uso:
    python tablas/generar_categorias.py

Puede interrumpirse y reanudarse: solo procesa productos sin categoría.
"""

import os
import sys
import time
import json
import logging
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("google").setLevel(logging.ERROR)

import mysql.connector
from google import genai
from dotenv import load_dotenv

# ─── Config ───────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "")
GEMINI_API_KEY1 = os.getenv("GEMINI_API_KEY1", "")

_API_KEYS = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY1] if k]
if not _API_KEYS:
    print("ERROR: No hay ninguna GEMINI_API_KEY definida en el .env")
    sys.exit(1)

DB_CONFIG = {
    "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER",     "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "inventario_local"),
    "charset":  "utf8mb4",
}

BATCH_SIZE = 50
PAUSE_SEG  = 3
REINTENTOS = 3
MODELO     = "gemini-3.5-flash-lite"

# Categorías existentes en el sistema (para que Gemini solo use estas)
CATEGORIAS = [
    "ACCESORIOS", "DAHUA CÁMARAS", "TP-LINK", "FIBRA", "SOLAR", "RUIJIE",
    "UTP", "HIKVISION", "ALARMAS", "IMOU", "DAHUA XVR - NVR", "UBIQUITI",
    "BATERIAS", "ACCESO", "MIKROTIK", "TENDA", "EZVIZ", "ZKTECO", "Tapo",
    "DEXSON", "UPS", "VSOL", "VIGI", "HAGROY", "WESTERN DIGITAL",
    "INCENDIO DAHUA", "IMOU POE", "HILOOK", "GABINETES", "DOMOTICA",
    "INTELBRAS", "INSTALACIÓN", "ECOFLOW", "Dahua Wifi", "MIMOSA", "NETION",
    "TXPRO", "DAHUA VIDEO PORTERO", "WI-TEK", "RF ELEMENTS", "UNIVIEW",
    "DAHUA MONITORES", "DAHUA ACCESO Y ASISTENCIA", "AJAX", "JFL",
    "Mercusys Wifi", "DISCO SOLIDO", "NETPOINT", "ALGCOM", "CAMBIUM",
    "HUAWEI", "D-LINK", "GRANDSTREAM", "GREENPOINT", "DAHUA ALARMA",
    "DAHUA REDES", "NGTECO", "STARLINK", "INCENDIO", "TOSHIBA", "XIAOMI",
    "ARUBA", "DAHUA ACCESORIOS", "LYNX", "SEAGATE", "GPS",
]

PROMPT_SISTEMA = f"""Eres un clasificador de productos de tecnología para una empresa
colombiana llamada Alianxa. Tu tarea es asignar la categoría correcta a cada producto
basándote en su nombre y referencia.

CATEGORÍAS DISPONIBLES (usa EXACTAMENTE una de estas, respetando mayúsculas/minúsculas):
{json.dumps(CATEGORIAS, ensure_ascii=False)}

Reglas:
- Asigna la categoría que mejor describe el producto.
- Si el producto es de una marca que tiene su propia categoría (ej. HIKVISION, DAHUA,
  TP-LINK), usa esa categoría de marca.
- Si no encaja en ninguna categoría de marca, usa la categoría funcional más cercana
  (ACCESORIOS, SOLAR, UPS, BATERIAS, FIBRA, etc.).
- Responde SOLO con un JSON válido, sin texto extra:

[
  {{"id": "ID_DEL_PRODUCTO", "categoria": "NOMBRE_CATEGORIA"}},
  ...
]
"""

# ─── Gemini con rotación de keys ──────────────────────────
_key_index = 0

def _make_client(key: str):
    return genai.Client(api_key=key)

client = _make_client(_API_KEYS[_key_index])


def generar_batch(productos: list[dict]) -> dict[str, str]:
    """Llama a Gemini y retorna {id_product: categoria}."""
    global client, _key_index

    lista = "\n".join(
        f'- id="{p["id_product"]}" nombre="{p["nombre"]}" ref="{p["ref_producto"]}"'
        for p in productos
    )
    prompt = f"{PROMPT_SISTEMA}\n\nProductos:\n{lista}"

    for intento in range(1, REINTENTOS + 1):
        try:
            resp  = client.models.generate_content(model=MODELO, contents=prompt)
            texto = resp.text.strip()

            if texto.startswith("```"):
                texto = texto.split("```")[1]
                if texto.lower().startswith("json"):
                    texto = texto[4:]
                texto = texto.strip()

            items = json.loads(texto)
            return {item["id"]: item["categoria"] for item in items}

        except Exception as e:
            msg = str(e)
            if "429" in msg and _key_index + 1 < len(_API_KEYS):
                _key_index += 1
                client = _make_client(_API_KEYS[_key_index])
                print(f"  [Key agotada] Rotando a key #{_key_index + 1}...")
                continue
            print(f"  [Intento {intento}/{REINTENTOS}] Error: {msg[:120]}")
            if intento < REINTENTOS:
                time.sleep(3)

    return {}


def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT id_product, nombre, ref_producto
        FROM productos
        WHERE categoria IS NULL OR categoria = ''
        ORDER BY id_product
    """)
    pendientes = cur.fetchall()
    total = len(pendientes)

    if total == 0:
        print("Todos los productos ya tienen categoría. Nada que hacer.")
        cur.close(); conn.close()
        return

    print(f"Productos sin categoría: {total}")
    print(f"Lotes de {BATCH_SIZE} | {((total + BATCH_SIZE - 1) // BATCH_SIZE)} llamadas a la API\n")

    actualizados = 0
    errores      = 0

    for i in range(0, total, BATCH_SIZE):
        lote = pendientes[i : i + BATCH_SIZE]
        num_lote    = i // BATCH_SIZE + 1
        total_lotes = (total + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"Lote {num_lote}/{total_lotes} — {len(lote)} productos")

        resultado = generar_batch(lote)

        if not resultado:
            print(f"  Sin resultado, se omite.")
            errores += len(lote)
        else:
            for id_product, categoria in resultado.items():
                # Validar que la categoría devuelta esté en la lista
                if categoria not in CATEGORIAS:
                    print(f"  [IGNORADO] {id_product}: categoria '{categoria}' no reconocida")
                    errores += 1
                    continue
                try:
                    cur.execute(
                        "UPDATE productos SET categoria = %s WHERE id_product = %s",
                        (categoria, id_product),
                    )
                    actualizados += 1
                    print(f"  ✓ {id_product}: {categoria}")
                except Exception as e:
                    print(f"  [DB ERROR] {id_product}: {e}")
                    errores += 1

            conn.commit()

        if i + BATCH_SIZE < total:
            time.sleep(PAUSE_SEG)

    cur.close()
    conn.close()

    print(f"\n{'='*50}")
    print(f"Finalizo. Actualizados: {actualizados} | Errores: {errores}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
