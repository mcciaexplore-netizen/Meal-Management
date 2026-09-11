import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_api import ApiHarness


ROOT = Path(__file__).resolve().parents[2]
INSTALL_ASSETS = (
    "manifest.webmanifest", "icon-192.png", "icon-512.png",
    "icon-maskable-512.png", "apple-touch-icon.png",
)


class ScannerInstallAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        frontend = root / "frontend" / "dist" / "scanner"
        frontend.mkdir(parents=True)
        for asset in INSTALL_ASSETS:
            shutil.copyfile(ROOT / "frontend" / "pwa" / asset, frontend / asset)
        shutil.copyfile(ROOT / "frontend" / "scan.html", frontend / "index.html")
        with patch("meal_management.scanner_api.__file__", str(root / "backend" / "meal_management" / "scanner_api.py")):
            self.harness = ApiHarness(application="scanner")
        self.addCleanup(self.harness.client.close)

    def test_manifest_and_icons_are_public_without_scanner_activation_or_database_access(self):
        for asset in INSTALL_ASSETS:
            with self.subTest(asset=asset):
                response = self.harness.client.get("/assets/" + asset)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, (ROOT / "frontend" / "pwa" / asset).read_bytes())
                expected = "application/manifest+json" if asset.endswith(".webmanifest") else "image/png"
                self.assertEqual(response.headers["content-type"].split(";")[0], expected)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertNotIn("set-cookie", response.headers)
        self.harness.services.database.transaction.assert_not_called()
        self.assertEqual(self.harness.staff.authentication_calls, [])

    def test_installation_assets_do_not_grant_scanner_write_access(self):
        response = self.harness.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/assets/manifest.webmanifest"', response.text)
        self.assertIn('id="scanner-install"', response.text)
        response = self.harness.client.post(
            "/api/scanner/read",
            json={"request_id": "10000000-0000-4000-8000-000000000001", "token": "x" * 43},
            headers={"Origin": self.harness.runtime.app_origin},
        )
        self.assertEqual(response.status_code, 401)
        self.harness.services.database.transaction.assert_not_called()

    def test_admin_does_not_publish_scanner_installation_assets(self):
        harness = ApiHarness()
        self.addCleanup(harness.client.close)
        for asset in INSTALL_ASSETS:
            with self.subTest(asset=asset):
                self.assertEqual(harness.client.get("/assets/" + asset).status_code, 404)
        harness.services.database.transaction.assert_not_called()
