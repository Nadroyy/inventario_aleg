import unittest

from proyecto_web.app import normalize_document_type


class DocumentTypeAliasTest(unittest.TestCase):
    def test_ppt_maps_to_ppt_and_accepts_pep_alias(self):
        self.assertEqual(normalize_document_type("PPT"), "PPT")
        self.assertEqual(normalize_document_type("ppt"), "PPT")
        self.assertEqual(normalize_document_type("Permiso"), "PPT")
        self.assertEqual(normalize_document_type("PEP"), "PEP")


if __name__ == "__main__":
    unittest.main()
