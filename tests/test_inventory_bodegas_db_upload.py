import unittest
from unittest.mock import patch

from excel_ai.inventario_bodegas_db import OUTPUT_FILE, _upload_to_drive


class InventoryBodegasDriveUploadTest(unittest.TestCase):
    @patch("excel_ai.inventario_bodegas_db.MediaFileUpload")
    @patch("excel_ai.inventario_bodegas_db.get_drive_service")
    @patch("excel_ai.inventario_bodegas_db.DRIVE_FILE_ID", "test-drive-file-id")
    def test_updates_existing_xlsx_in_drive_without_changing_its_id(
        self, get_drive_service, media_file_upload
    ):
        drive = get_drive_service.return_value
        drive.files.return_value.update.return_value.execute.return_value = {
            "id": "test-drive-file-id",
            "name": "inventory-demo",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "webViewLink": "https://drive.google.com/file/d/test-drive-file-id",
        }

        _upload_to_drive()

        media_file_upload.assert_called_once_with(
            str(OUTPUT_FILE),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            resumable=True,
        )
        drive.files.return_value.update.assert_called_once_with(
            fileId="test-drive-file-id",
            media_body=media_file_upload.return_value,
            fields="id,name,mimeType,webViewLink",
        )

    @patch("excel_ai.inventario_bodegas_db.DRIVE_FILE_ID", "")
    def test_requires_drive_file_id_before_upload(self):
        with self.assertRaisesRegex(RuntimeError, "INVENTORY_BODEGAS_DRIVE_FILE_ID"):
            _upload_to_drive()
