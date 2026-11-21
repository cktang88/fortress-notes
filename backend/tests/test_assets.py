import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import make_settings, use_settings


class AssetUploadTests(unittest.TestCase):
    def test_upload_display_names_lose_their_path(self) -> None:
        from app import assets

        self.assertEqual(assets.safe_filename(r"..\private\meeting notes.mp3"), "meeting notes.mp3")
        self.assertEqual(assets.safe_filename(".."), "file")

    def test_pasted_image_keeps_its_type_when_the_header_has_parameters_or_capitals(self) -> None:
        from app import assets

        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory))
            with use_settings(settings):
                for content_type in ("image/jpeg; charset=binary", "IMAGE/JPEG", "image/webp"):
                    path = assets.upload_path(content_type)
                    expected = ".webp" if "webp" in content_type else ".jpg"
                    self.assertEqual(path.suffix, expected, content_type)


if __name__ == "__main__":
    unittest.main()
