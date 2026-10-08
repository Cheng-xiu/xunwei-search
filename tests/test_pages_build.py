import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin

from scripts.build_pages import BuildError, STATIC_FILES, build_pages, normalize_api_base


class PagesBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        self.web = self.root / "web"
        self.web.mkdir()
        assets = {
            "index.html": '<!doctype html><a href="./">Home</a><link rel="stylesheet" href="./styles.css"><script src="./deployment-config.js"></script><script src="./connection.js"></script><script src="./app.js"></script>',
            "app.js": "window.applicationLoaded = true;\n",
            "styles.css": "body { color: #123; }\n",
            "connection.js": "window.connectionLoaded = true;\n",
            "deployment-config.js": 'window.XUNWEI_DEPLOYMENT = { mode: "local", apiBase: "" };\n',
            "platforms.json": json.dumps({"items": [{"id": "example", "label": "Public example", "domains": ["example.com"]}]}),
        }
        for name, value in assets.items():
            (self.web / name).write_text(value, encoding="utf-8")

    def configuration(self, output):
        text = (output / "deployment-config.js").read_text(encoding="utf-8")
        return json.loads(text.split("window.XUNWEI_DEPLOYMENT = ", 1)[1].rstrip().removesuffix(";"))

    def test_exact_allowlist_excludes_backend_settings_history_and_user_exports(self):
        canary = "PRIVATE_IMPORTED_DOCUMENT_CANARY"
        for relative in (".local/settings.json", ".local/history.json", ".local/search.db", "search_app/server.py", "tests/test-private.json", "export.json", "web/private-export.json", "web/history.js"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canary + " " + "sk-" + "A" * 44, encoding="utf-8")
        read_paths = []
        original = Path.read_text
        def read(path, *args, **kwargs):
            read_paths.append(path)
            self.assertEqual(path.parent, self.web)
            self.assertIn(path.name, STATIC_FILES)
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            output = build_pages(self.root)
        self.assertEqual({path.name for path in read_paths}, set(STATIC_FILES))
        self.assertEqual({path.name for path in output.iterdir()}, set(STATIC_FILES) | {".nojekyll"})
        self.assertTrue(all(path.is_file() for path in output.iterdir()))
        self.assertNotIn(canary, "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir()))
        self.assertEqual(self.configuration(output), {"mode": "pages", "apiBase": ""})

    def test_default_config_ignores_secret_environment_and_does_not_modify_source(self):
        original = (self.web / "deployment-config.js").read_text(encoding="utf-8")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-" + "B" * 44, "SERVICE_TOKEN": "test-private-token"}):
            output = build_pages(self.root)
        self.assertEqual(self.configuration(output), {"mode": "pages", "apiBase": ""})
        self.assertEqual((self.web / "deployment-config.js").read_text(encoding="utf-8"), original)
        self.assertNotIn("test-private-token", (output / "deployment-config.js").read_text(encoding="utf-8"))

    def test_explicit_public_https_base_has_no_token_field(self):
        output = build_pages(self.root, "https://api.example.com/search-service/")
        self.assertEqual(self.configuration(output), {"mode": "pages", "apiBase": "https://api.example.com/search-service"})

    def test_relative_resources_work_at_a_project_subpath(self):
        output = build_pages(self.root)
        page_url = "https://owner.github.io/smart-search/"
        for name in ("styles.css", "deployment-config.js", "connection.js", "app.js"):
            self.assertIn('"./' + name + '"', (output / "index.html").read_text(encoding="utf-8"))
            self.assertEqual(urljoin(page_url, "./" + name), page_url + name)

    def test_rebuild_removes_stale_exports_from_disposable_docs(self):
        output = build_pages(self.root)
        (output / "export.json").write_text("sensitive stale export", encoding="utf-8")
        (output / "old-data").mkdir()
        (output / "old-data" / "history.json").write_text("old private history", encoding="utf-8")
        build_pages(self.root)
        self.assertEqual({path.name for path in output.iterdir()}, set(STATIC_FILES) | {".nojekyll"})

    def test_root_resource_unknown_asset_and_external_script_are_rejected_before_output_changes(self):
        output = build_pages(self.root)
        before = (output / "index.html").read_text(encoding="utf-8")
        for source in ('<script src="/app.js"></script>', '<script src="./history.js"></script>', '<script src="https://cdn.example/app.js"></script>'):
            with self.subTest(source=source):
                (self.web / "index.html").write_text(source, encoding="utf-8")
                with self.assertRaises(BuildError):
                    build_pages(self.root)
                self.assertEqual((output / "index.html").read_text(encoding="utf-8"), before)

    def test_missing_frontend_file_preserves_last_good_output(self):
        output = build_pages(self.root)
        (self.web / "connection.js").unlink()
        with self.assertRaisesRegex(BuildError, "connection.js"):
            build_pages(self.root)
        self.assertTrue((output / "connection.js").is_file())

    def test_credential_shaped_source_is_refused_without_echoing_credential(self):
        fake = "sk-" + "C" * 44
        (self.web / "app.js").write_text("const accidentalKey = '" + fake + "';", encoding="utf-8")
        with self.assertRaises(BuildError) as caught:
            build_pages(self.root)
        self.assertNotIn(fake, str(caught.exception))
        self.assertFalse((self.root / "docs").exists())

    def test_malformed_or_escaped_secret_catalog_is_refused(self):
        for text in ("[]", "{bad json", '{"items":[{"key":"sk-' + "\\u0044" * 44 + '"}]}'):
            with self.subTest(text=text):
                (self.web / "platforms.json").write_text(text, encoding="utf-8")
                with self.assertRaises(BuildError):
                    build_pages(self.root)

    def test_linked_source_is_not_followed(self):
        asset = self.web / "app.js"
        external = self.root.parent / "private-settings.json"
        external.write_text("private backend configuration", encoding="utf-8")
        asset.unlink()
        try:
            asset.symlink_to(external)
        except OSError:
            self.skipTest("This account cannot create symlinks")
        with self.assertRaises(BuildError):
            build_pages(self.root)
        self.assertEqual(external.read_text(encoding="utf-8"), "private backend configuration")

    def test_linked_output_is_not_replaced_or_deleted(self):
        external = self.root.parent / "unrelated"
        external.mkdir()
        (external / "keep.txt").write_text("keep me", encoding="utf-8")
        try:
            (self.root / "docs").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("This account cannot create symlinks")
        with self.assertRaises(BuildError):
            build_pages(self.root)
        self.assertEqual((external / "keep.txt").read_text(encoding="utf-8"), "keep me")


class PublicApiBaseTests(unittest.TestCase):
    def test_https_and_explicit_loopback_previews(self):
        self.assertEqual(normalize_api_base(""), "")
        self.assertEqual(normalize_api_base("https://API.EXAMPLE.COM/prefix/"), "https://api.example.com/prefix")
        for url in ("http://localhost:8765", "http://127.0.0.1:8765", "https://[::1]:8765", "https://[2606:4700:4700::1111]"):
            self.assertEqual(normalize_api_base(url), url)

    def test_no_credentials_tokens_query_fragment_remote_http_or_url_tricks(self):
        for url in ("http://api.example.com", "http://localhost.evil.example", "http://127.0.0.1.evil.example", "http://127.2.3.4:8765", "http://127.0.0.2", "http://[::1]:8765", "http://192.168.1.1", "//api.example.com", "javascript:alert(1)", "https://user:password@example.com", "https://example.com/?token=private", "https://example.com/#private", "https://example.com\\@evil.example", "https://example.com/%0Apath", "https://example.com:99999", "https://example.com/sk-" + "E" * 44):
            with self.subTest(url=url), self.assertRaises(BuildError) as caught:
                normalize_api_base(url)
            self.assertNotIn(url, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
