from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LocalIconAssetTests(unittest.TestCase):
    def test_lucide_is_loaded_from_the_bundled_web_assets(self):
        index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        asset = ROOT / "web" / "assets" / "vendor" / "lucide-1.16.0.min.js"

        self.assertIn('src="/assets/vendor/lucide-1.16.0.min.js"', index)
        self.assertNotIn("unpkg.com/lucide", index)
        self.assertTrue(asset.is_file())
        self.assertIn("lucide v1.16.0", asset.read_text(encoding="utf-8")[:200])


if __name__ == "__main__":
    unittest.main()
