import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin

from scripts.build_pages import (BuildError, STATIC_FILES, build_pages, main,
                                 normalize_api_base, normalize_owner_ai, owner_ai_from_environment)


OWNER = {"baseURL": "https://model.example/v1", "model": "public-test-model", "apiKey": "sk-" + "P" * 44}
OWNER_ENV = {"XUNWEI_PAGES_PUBLIC_OWNER_API": "1", "XUNWEI_PAGES_OWNER_BASE_URL": OWNER['baseURL'],
             "XUNWEI_PAGES_OWNER_MODEL": OWNER['model'], "XUNWEI_PAGES_OWNER_API_KEY": OWNER['apiKey']}


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

    def test_explicit_owner_preset_only_enters_generated_config_and_never_changes_source(self):
        original = {path.name: path.read_text(encoding='utf-8') for path in self.web.iterdir()}
        output = build_pages(self.root, 'https://search.example', owner_ai=OWNER)
        self.assertEqual(self.configuration(output), {'mode': 'pages', 'apiBase': 'https://search.example', 'ownerAI': OWNER})
        containing_key = [path.name for path in output.iterdir() if OWNER['apiKey'] in path.read_text(encoding='utf-8')]
        self.assertEqual(containing_key, ['deployment-config.js'])
        self.assertIn('every visitor can read', (output / 'deployment-config.js').read_text(encoding='utf-8'))
        self.assertEqual({path.name: path.read_text(encoding='utf-8') for path in self.web.iterdir()}, original)
        # A later ordinary build removes the public preset even after an opt-in build.
        build_pages(self.root)
        self.assertEqual(self.configuration(output), {'mode': 'pages', 'apiBase': ''})
        self.assertNotIn(OWNER['apiKey'], '\n'.join(path.read_text(encoding='utf-8') for path in output.iterdir()))

    def test_library_build_ignores_even_opted_in_cli_environment_without_explicit_argument(self):
        with patch.dict('os.environ', OWNER_ENV, clear=True):
            output = build_pages(self.root)
        self.assertNotIn('ownerAI', self.configuration(output))
        self.assertNotIn(OWNER['apiKey'], '\n'.join(path.read_text(encoding='utf-8') for path in output.iterdir()))

    def test_owner_opt_in_does_not_relax_static_asset_scan(self):
        for name in ('app.js', 'deployment-config.js', 'platforms.json'):
            path = self.web / name
            original = path.read_text(encoding='utf-8')
            with self.subTest(name=name):
                path.write_text('const accidentalKey = ' + json.dumps(OWNER['apiKey']) + ';', encoding='utf-8')
                with self.assertRaises(BuildError) as caught:
                    build_pages(self.root, owner_ai=OWNER)
                self.assertNotIn(OWNER['apiKey'], str(caught.exception))
            path.write_text(original, encoding='utf-8')

    def test_invalid_owner_preserves_previous_build_and_has_value_free_error(self):
        output = build_pages(self.root)
        original = (output / 'deployment-config.js').read_text(encoding='utf-8')
        invalid = [[], {}, {**OWNER, 'extra': 'unapproved'}, {**OWNER, 'baseURL': ''},
                   {**OWNER, 'baseURL': 'http://localhost:8765/v1'},
                   {**OWNER, 'baseURL': 'https://user:secret@model.example/v1'},
                   {**OWNER, 'baseURL': 'https://model.example/v1?key=' + OWNER['apiKey']},
                   {**OWNER, 'baseURL': 'https://model.example/v1?'},
                   {**OWNER, 'baseURL': 'https://model.example/v1#'},
                   {**OWNER, 'baseURL': 'https://model.example/%0Apath'},
                   {**OWNER, 'model': ''}, {**OWNER, 'model': 'm' * 151}, {**OWNER, 'model': 'test\nmodel'},
                   {**OWNER, 'model': OWNER['apiKey']}, {**OWNER, 'apiKey': ''},
                   {**OWNER, 'apiKey': OWNER['apiKey'] + '\n'}, {**OWNER, 'apiKey': 'contains space'},
                   {**OWNER, 'apiKey': '密钥'}, {**OWNER, 'apiKey': 'x' * 4097}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(BuildError) as caught:
                build_pages(self.root, owner_ai=value)
            self.assertNotIn(OWNER['apiKey'], str(caught.exception))
            self.assertEqual((output / 'deployment-config.js').read_text(encoding='utf-8'), original)

    def test_owner_strings_are_serialized_as_data(self):
        owner = {**OWNER, 'model': 'provider/model-"quoted"', 'apiKey': 'opaque-"quoted"-\\key'}
        output = build_pages(self.root, owner_ai=owner)
        self.assertEqual(self.configuration(output)['ownerAI'], owner)

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


class PublicOwnerEnvironmentTests(unittest.TestCase):
    def test_cli_default_does_not_read_generic_model_keys(self):
        self.assertIsNone(owner_ai_from_environment({'AI_API_KEY': OWNER['apiKey'], 'OPENAI_API_KEY': OWNER['apiKey']}))
        self.assertIsNone(owner_ai_from_environment({'XUNWEI_PAGES_PUBLIC_OWNER_API': '0'}))

    def test_owner_fields_without_explicit_opt_in_are_rejected_without_echo(self):
        for flag in ('', '0', 'true'):
            environment = {**OWNER_ENV, 'XUNWEI_PAGES_PUBLIC_OWNER_API': flag}
            with self.subTest(flag=flag), self.assertRaises(BuildError) as caught:
                owner_ai_from_environment(environment)
            self.assertIn('explicitly', str(caught.exception))
            self.assertNotIn(OWNER['apiKey'], str(caught.exception))

    def test_incomplete_opt_in_is_rejected_without_echo(self):
        for missing in ('XUNWEI_PAGES_OWNER_BASE_URL', 'XUNWEI_PAGES_OWNER_MODEL', 'XUNWEI_PAGES_OWNER_API_KEY'):
            environment = dict(OWNER_ENV)
            del environment[missing]
            with self.subTest(missing=missing), self.assertRaises(BuildError) as caught:
                owner_ai_from_environment(environment)
            self.assertIn('incomplete', str(caught.exception))
            self.assertNotIn(OWNER['apiKey'], str(caught.exception))

    def test_valid_opt_in_passes_exact_preset_to_cli_build_without_printing_values(self):
        output = StringIO()
        with patch.dict('os.environ', OWNER_ENV, clear=True), patch('scripts.build_pages.build_pages') as build, redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(build.call_args.args[2], OWNER)
        self.assertIn('Every visitor can read', output.getvalue())
        for value in OWNER.values():
            self.assertNotIn(value, output.getvalue())

    def test_invalid_cli_configuration_never_starts_build_or_prints_supplied_values(self):
        environment = {**OWNER_ENV, 'XUNWEI_PAGES_OWNER_MODEL': OWNER['apiKey']}
        output = StringIO()
        with patch.dict('os.environ', environment, clear=True), patch('scripts.build_pages.build_pages') as build, redirect_stdout(output):
            self.assertEqual(main(), 1)
        build.assert_not_called()
        self.assertIn('Pages build failed', output.getvalue())
        self.assertNotIn(OWNER['apiKey'], output.getvalue())

    def test_https_model_base_and_bounded_opaque_fields(self):
        normalized = normalize_owner_ai({**OWNER, 'baseURL': 'https://MODEL.EXAMPLE/v1/', 'model': '  model/test  '})
        self.assertEqual(normalized, {**OWNER, 'model': 'model/test'})
        self.assertEqual(len(normalize_owner_ai({**OWNER, 'apiKey': 'x' * 4096})['apiKey']), 4096)


if __name__ == "__main__":
    unittest.main()
