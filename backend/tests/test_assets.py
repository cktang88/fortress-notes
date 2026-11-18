import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.assets import save_upload

from support import make_settings, use_settings


class AssetUploadTests(unittest.TestCase):
    def test_upload_uses_safe_display_name_and_generated_media_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory))
            asset_directory = settings.assets_path
            with use_settings(settings):
                url, name, path = save_upload(
                    b"audio data", r"..\private\meeting notes.mp3", "audio/mpeg"
                )

            self.assertEqual(name, "meeting notes.mp3")
            self.assertRegex(url, r"^/media/[0-7][0-9A-HJ-KM-NP-TV-Z]{25}\.mp3$")
            self.assertEqual(path.parent, asset_directory)
            self.assertEqual(path.read_bytes(), b"audio data")

    def test_untrusted_filename_extension_is_not_used_for_stored_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory))
            asset_directory = settings.assets_path
            with use_settings(settings):
                url, name, path = save_upload(b"plain text", "report.html", "text/plain")

            self.assertEqual(name, "report.html")
            self.assertTrue(url.endswith(".bin"))
            self.assertEqual(path.suffix, ".bin")

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
