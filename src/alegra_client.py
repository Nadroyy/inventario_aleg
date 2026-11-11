"""Cliente base para la API de Alegra con paginación automática y checkpoint."""

import json
import os
import time
import requests
from datetime import date
from pathlib import Path
from requests.auth import HTTPBasicAuth

from src.config import ALEGRA_EMAIL, ALEGRA_API_TOKEN, BASE_URL, MAX_LIMIT, REQUEST_DELAY

# Directorio para guardar checkpoints por endpoint+status
_CHECKPOINT_DIR = Path("data/.checkpoints")


def _checkpoint_path(endpoint: str, params: dict) -> Path:
    """Genera un nombre de archivo de checkpoint único para endpoint+params clave."""
    status = params.get("status", "all")
    safe = endpoint.replace("/", "_")
    return _CHECKPOINT_DIR / f"{safe}_{status}.json"


def _load_checkpoint(path: Path) -> dict | None:
    """Carga checkpoint del día. Si es de otro día, lo descarta."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            cp = json.load(f)
        if cp.get("date") == str(date.today()):
            return cp
        path.unlink(missing_ok=True)
    except Exception:
        path.unlink(missing_ok=True)
    return None


def _save_checkpoint(path: Path, records: list, next_start: int) -> None:
    """Guarda el checkpoint de forma atómica."""
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    cp = {"date": str(date.today()), "records": records, "next_start": next_start}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False)
    tmp.replace(path)


def _clear_checkpoint(path: Path) -> None:
    path.unlink(missing_ok=True)


class AlegraClient:
    """Cliente HTTP para interactuar con la API REST de Alegra."""

    def __init__(self):
        self.auth = HTTPBasicAuth(ALEGRA_EMAIL, ALEGRA_API_TOKEN)
        self.base_url = BASE_URL
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Accept": "application/json"})

    # Errores temporales de Alegra que SÍ vale la pena reintentar.
    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def get(self, endpoint, params=None, max_retries=5):
        """Realiza un GET a la API de Alegra, reintentando ante errores temporales.

        Alegra devuelve 503/429/500 de forma intermitente. Sin reintentos, la
        paginación se cortaba y guardaba un items.json INCOMPLETO (precios/stock
        desactualizados). Aquí reintentamos con backoff exponencial.

        Retorna:
          - dict/list con los datos si status 200
          - None solo si es un error definitivo (401, 404, etc.) o se agotan los reintentos
        """
        url = f"{self.base_url}/{endpoint}"
        espera = 3  # segundos, se duplica en cada reintento

        for intento in range(1, max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                # Error de red/timeout: tratar como temporal y reintentar
                if intento < max_retries:
                    print(f"  ⚠ Error de red ({e.__class__.__name__}), reintentando en {espera}s... ({intento}/{max_retries})")
                    time.sleep(espera)
                    espera *= 2
                    continue
                print(f"  ✗ Error de red tras {max_retries} intentos: {e}")
                return None

            if response.status_code == 401:
                print("ERROR 401: Credenciales inválidas.")
                print("Verifica que tu token API sea correcto (no es la contraseña de login).")
                return None

            if response.status_code == 200:
                return response.json()

            # Errores temporales -> reintentar con backoff
            if response.status_code in self._RETRY_STATUS and intento < max_retries:
                print(f"  ⚠ Alegra respondió {response.status_code}, reintentando en {espera}s... ({intento}/{max_retries})")
                time.sleep(espera)
                espera *= 2
                continue

            # Error definitivo o reintentos agotados
            print(f"ERROR {response.status_code}: {response.text[:200]}")
            return None

        return None

    def get_all_paginated(self, endpoint, params=None, limit=MAX_LIMIT, fail_on_first_page=False):
        """
        Obtiene TODOS los registros de un endpoint paginando automáticamente.

        Con checkpoint: si la descarga se interrumpe a mitad (timeout, reinicio
        del contenedor), la próxima llamada del mismo día retoma desde la última
        página guardada en lugar de empezar desde cero.

        La API de Alegra limita a 30 registros por página.
        """
        if params is None:
            params = {}

        # ── Checkpoint: reanudar si existe uno del día ────────────────────────
        cp_path = _checkpoint_path(endpoint, params)
        cp = _load_checkpoint(cp_path)
        if cp:
            all_records = cp["records"]
            start = cp["next_start"]
            print(f"  ✓ Checkpoint: {len(all_records)} registros ya descargados, "
                  f"retomando desde start={start}...")
        else:
            all_records = []
            start = 0

        while True:
            params["start"] = start
            params["limit"] = limit

            data = self.get(endpoint, params=params)

            # Fallo definitivo A MITAD de la paginación: guardar checkpoint y abortar.
            # El llamador no sobreescribe items.json con datos truncados.
            if data is None:
                if all_records or fail_on_first_page:
                    # Guardar checkpoint para reanudar en la próxima corrida
                    _save_checkpoint(cp_path, all_records, start)
                    raise RuntimeError(
                        f"La descarga de '{endpoint}' falló en start={start} tras varios "
                        f"reintentos. Se guardó checkpoint con {len(all_records)} registros. "
                        f"La próxima corrida retomará desde aquí automáticamente."
                    )
                break

            # Página vacía: fin normal de la paginación
            if not data:
                break

            all_records.extend(data)
            print(f"  → {len(all_records)} registros obtenidos...")

            # Guardar checkpoint después de cada página
            next_start = start + limit
            _save_checkpoint(cp_path, all_records, next_start)

            # Si devolvió menos del límite, ya no hay más páginas
            if len(data) < limit:
                break

            start = next_start
            time.sleep(REQUEST_DELAY)

        # Descarga completa — limpiar checkpoint
        _clear_checkpoint(cp_path)
        return all_records
