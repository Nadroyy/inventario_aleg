"""Configuración del proyecto - carga variables desde .env"""

import os
import sys
from dotenv import load_dotenv

# En Windows la consola usa cp1252 por defecto y falla al imprimir
# caracteres Unicode (→, ✓, etc.). Forzamos UTF-8 en la salida.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Cargar variables de entorno desde .env
load_dotenv()

ALEGRA_EMAIL = os.getenv("ALEGRA_EMAIL")
ALEGRA_API_TOKEN = os.getenv("ALEGRA_API_TOKEN")
BASE_URL = "https://api.alegra.com/api/v1"

# Límite máximo permitido por la API de Alegra por página
MAX_LIMIT = 30

# Pausa entre requests para no saturar la API (segundos).
# Con 0.5s la tasa máxima es 120 req/min, bien por debajo del límite de 150.
REQUEST_DELAY = 0.5


def validate_config():
    """Valida que las credenciales estén configuradas."""
    if not ALEGRA_EMAIL or ALEGRA_EMAIL == "tu_email_aqui":
        print("ERROR: Configura ALEGRA_EMAIL en el archivo .env")
        sys.exit(1)
    if not ALEGRA_API_TOKEN or ALEGRA_API_TOKEN == "tu_token_api_aqui":
        print("ERROR: Configura ALEGRA_API_TOKEN en el archivo .env")
        print("\nPara obtener tu token API:")
        print("  1. Ingresa a Alegra con tus credenciales")
        print("  2. Ve a Configuración → Integraciones → API")
        print("  3. Copia el token que aparece ahí")
        sys.exit(1)
