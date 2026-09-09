import unittest
from xml.etree import ElementTree

from cryptography.fernet import Fernet

from meal_management.errors import DomainError
from meal_management.security import QrRenderer, TokenVault, generate_token


class InstalledCryptoTests(unittest.TestCase):
    def test_vault_roundtrip_key_rotation_and_tamper_rejection(self):
        older, newer = Fernet.generate_key(), Fernet.generate_key()
        token = generate_token()
        old_vault = TokenVault([older])
        ciphertext = old_vault.encrypt(token)
        rotated = TokenVault([newer, older])
        self.assertEqual(rotated.decrypt(ciphertext), token)
        self.assertNotIn(token.encode(), ciphertext)
        new_ciphertext = rotated.encrypt(token)
        self.assertEqual(TokenVault([newer]).decrypt(new_ciphertext), token)
        with self.assertRaises(DomainError):
            old_vault.decrypt(new_ciphertext)
        with self.assertRaises(DomainError):
            rotated.decrypt(ciphertext[:-5] + b"xxxxx")

    def test_renderer_produces_svg_paths_without_plaintext_token(self):
        token = generate_token()
        svg = QrRenderer().render(token)
        document = ElementTree.fromstring(svg)
        self.assertEqual(document.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertTrue(document.findall(".//{http://www.w3.org/2000/svg}path"))
        self.assertNotIn(token, svg)


if __name__ == "__main__":
    unittest.main()
