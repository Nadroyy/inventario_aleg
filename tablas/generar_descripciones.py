"""
generar_descripciones.py
Genera descripciones comerciales para los productos de inventario_local
usando la API de Gemini. Procesa en lotes de 5 y actualiza la BD.

Uso:
    python tablas/generar_descripciones.py

Puede interrumpirse y reanudarse: solo procesa productos que aún
tienen la descripción vacía o con el texto genérico del dump.
"""

import os
import sys
import time
import json
import logging
import warnings
from pathlib import Path

# Suprimir warnings de thought_signature de la librería google-genai
warnings.filterwarnings("ignore")
logging.getLogger("google").setLevel(logging.ERROR)

import mysql.connector
from google import genai
from dotenv import load_dotenv

# ─── Config ───────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEY1 = os.getenv("GEMINI_API_KEY1", "")

# Lista de keys para rotación automática cuando se agota la cuota
_API_KEYS = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY1] if k]
if not _API_KEYS:
    print("ERROR: No hay ninguna GEMINI_API_KEY definida en el .env")
    sys.exit(1)

_key_index = 0  # índice de la key activa

DB_CONFIG = {
    "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "user":     os.getenv("MYSQL_USER",     "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "inventario_local"),
    "charset":  "utf8mb4",
}

BATCH_SIZE  = 50     # productos por llamada a Gemini (54 llamadas totales para 2693)
PAUSE_SEG   = 3      # segundos entre lotes (evita rate limit)
REINTENTOS  = 3      # reintentos por lote si Gemini falla
MODELO      = "gemini-3.5-flash-lite"   # rápido y económico vía REST

# ─── Gemini — cliente con rotación automática de keys ─────
def _make_client(key: str):
    return genai.Client(api_key=key)

client = _make_client(_API_KEYS[_key_index])

# ─── Prompt ───────────────────────────────────────────────
PROMPT_SISTEMA = """Eres un redactor de catálogos de tecnología para una empresa 
colombiana llamada Alianxa. Tu tarea es generar descripciones comerciales cortas 
(máximo 2 oraciones, máximo 60 palabras) para productos de inventario.

Las descripciones deben:
- Ser en español, claras y directas.
- Mencionar la función principal del producto.
- Incluir características técnicas clave si el nombre las indica (velocidad, 
  canales, megapixeles, capacidad, etc.).
- No inventar especificaciones que no estén en el nombre o referencia.
- No usar frases como "este producto", "el artículo", etc. Ir directo.

Responde SOLO con un JSON válido con este formato exacto, sin texto extra:
[
  {"id": "ID_DEL_PRODUCTO", "descripcion": "Texto de la descripción."},
  ...
]
"""

def generar_batch(productos: list[dict]) -> dict[str, str]:
    """
    Llama a Gemini con un lote de productos y retorna
    un dict {id_product: descripcion}. Con reintentos y rotación de keys.
    """
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

            # Limpiar posibles bloques markdown ```json ... ```
            if texto.startswith("```"):
                texto = texto.split("```")[1]
                if texto.lower().startswith("json"):
                    texto = texto[4:]
                texto = texto.strip()

            import json
            items = json.loads(texto)
            return {item["id"]: item["descripcion"] for item in items}

        except Exception as e:
            msg = str(e)
            # Si es cuota agotada (429), rotar a la siguiente key
            if "429" in msg and _key_index + 1 < len(_API_KEYS):
                _key_index += 1
                client = _make_client(_API_KEYS[_key_index])
                print(f"  [Key agotada] Rotando a key #{_key_index + 1}...")
                # Reintentar inmediatamente con la nueva key
                continue
            print(f"  [Intento {intento}/{REINTENTOS}] Error: {msg[:120]}")
            if intento < REINTENTOS:
                time.sleep(3)

    return {}


def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur  = conn.cursor(dictionary=True)

    # Traer solo los que necesitan descripción
    cur.execute("""
        SELECT id_product, nombre, ref_producto
        FROM productos
        WHERE descripcion IS NULL
           OR descripcion LIKE 'Creado al registrar%'
           OR descripcion = ''
        ORDER BY id_product
    """)
    pendientes = cur.fetchall()
    total = len(pendientes)

    if total == 0:
        print("Todos los productos ya tienen descripción. Nada que hacer.")
        cur.close(); conn.close()
        return

    print(f"Productos a procesar: {total}")
    print(f"Lotes de {BATCH_SIZE} | Pausa {PAUSE_SEG}s entre lotes\n")

    actualizados = 0
    errores      = 0

    for i in range(0, total, BATCH_SIZE):
        lote = pendientes[i : i + BATCH_SIZE]
        ids  = [p["id_product"] for p in lote]
        num_lote = i // BATCH_SIZE + 1
        total_lotes = (total + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"Lote {num_lote}/{total_lotes} — IDs: {ids}")

        resultado = generar_batch(lote)

        if not resultado:
            print(f"  Sin resultado para este lote, se omite.")
            errores += len(lote)
        else:
            for id_product, descripcion in resultado.items():
                try:
                    cur.execute(
                        "UPDATE productos SET descripcion = %s WHERE id_product = %s",
                        (descripcion, id_product),
                    )
                    actualizados += 1
                    print(f"  ✓ {id_product}: {descripcion[:60]}...")
                except Exception as e:
                    print(f"  [DB ERROR] {id_product}: {e}")
                    errores += 1

            conn.commit()

        # Pausa para no saturar la API (excepto en el último lote)
        if i + BATCH_SIZE < total:
            time.sleep(PAUSE_SEG)

    cur.close()
    conn.close()

    print(f"\n{'='*50}")
    print(f"Finalizó. Actualizados: {actualizados} | Errores: {errores}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
