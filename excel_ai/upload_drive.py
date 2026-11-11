"""
Sube el inventario_bodegas.xlsx de excel_ai/ a Google Drive como archivo .xlsx.

- Si ya existe un archivo subido anteriormente (guardado en excel_ai/drive_id.json),
  reemplaza el contenido en el mismo ID para no acumular archivos en Drive.
- Si no existe drive_id.json (primera ejecución), crea el archivo en Drive y guarda
  el nuevo file_id en drive_id.json.
- El archivo siempre permanece como .xlsx nativo — nunca se convierte a Google Sheet.

Uso:
    python -m excel_ai.upload_drive
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.http import MediaFileUpload
from proyecto_ecxel.share_catalog import get_drive_service

# ── Rutas ────────────────────────────────────────────────────────────────────
EXCEL_AI_DIR  = Path(__file__).resolve().parent
PROJECT_ROOT  = EXCEL_AI_DIR.parent
XLSX_FILE     = EXCEL_AI_DIR / "inventario_bodegas.xlsx"
DRIVE_ID_FILE = EXCEL_AI_DIR / "drive_id.json"

load_dotenv(PROJECT_ROOT / ".env")

# ── Constantes ────────────────────────────────────────────────────────────────
DRIVE_FILENAME = "INVENTARIO_BODEGAS_ALIANXA.xlsx"
MIMETYPE       = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _save_drive_id(file_id: str) -> None:
    """Guarda file_id en drive_id.json con escritura atómica."""
    tmp = str(DRIVE_ID_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"file_id": file_id}, f)
    os.replace(tmp, str(DRIVE_ID_FILE))


def upload():
    if not XLSX_FILE.exists():
        print(f"  ✗ No se encontró {XLSX_FILE}")
        print(f"    Genera el Excel primero con: python -m excel_ai.inventario_bodegas")
        return None

    drive = get_drive_service()

    # Leer file_id anterior si existe
    old_id = None
    if DRIVE_ID_FILE.exists():
        try:
            with open(DRIVE_ID_FILE, encoding="utf-8") as f:
                old_id = json.load(f).get("file_id")
        except Exception:
            old_id = None

    if old_id:
        # Actualizar contenido en el mismo ID — sin cambiar mimeType ni crear archivo nuevo
        print(f"  Actualizando archivo existente en Drive ({old_id})...")
        media = MediaFileUpload(str(XLSX_FILE), mimetype=MIMETYPE, resumable=True)
        try:
            file = drive.files().update(
                fileId=old_id,
                body={"name": DRIVE_FILENAME},
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()
            print(f"  ✓ Actualizado: {file.get('webViewLink', '')}")
            return file.get("id")
        except Exception as e:
            print(f"  ⚠ No se pudo actualizar ({e}), subiendo como nuevo archivo...")
            old_id = None

    # Primera ejecución o recuperación tras fallo — crear archivo nuevo
    GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
    print(f"  Subiendo nuevo archivo a Drive...")
    media = MediaFileUpload(str(XLSX_FILE), mimetype=MIMETYPE, resumable=True)
    body = {"name": DRIVE_FILENAME}
    if GDRIVE_FOLDER_ID:
        body["parents"] = [GDRIVE_FOLDER_ID]
    file = drive.files().create(
        body=body,
        media_body=media,
        fields="id,name,webViewLink",
    ).execute()

    new_id = file["id"]
    _save_drive_id(new_id)
    print(f"  ✓ Subido: {file.get('webViewLink', '')}")
    print(f"  ID guardado en: {DRIVE_ID_FILE}")
    return new_id


def main():
    print(f"Subiendo {XLSX_FILE.name} a Google Drive...")
    upload()


if __name__ == "__main__":
    main()
