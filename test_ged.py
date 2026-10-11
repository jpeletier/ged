import argparse
import http.cookiejar
import os
import tempfile
import unittest
from unittest.mock import patch

import ged


class AuthTests(unittest.TestCase):
    def test_expired_session_reauthenticates_once_and_retries_query(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ged.Client("school", directory)
            stale = (401, ged.BASE + "/school/ausencias/find", b'{}', "application/json")
            valid = (200, ged.BASE + "/school/ausencias/find", b'[]', "application/json")
            with patch.object(client, "request", side_effect=[stale, valid]) as request, \
                    patch.object(client, "login") as login:
                result = client.get("/school/ausencias/find", {"start": "2026-09-01"}, True)
                self.assertEqual(result[2], b"[]")
                login.assert_called_once()
                self.assertEqual(request.call_args_list[0], request.call_args_list[1])

    def test_valid_cookie_does_not_require_credentials_or_login(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ged.Client("school", directory)
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(client, "request", return_value=(200, ged.BASE + "/school", b"ok", "text/html")), \
                    patch.object(client, "login") as login:
                client.get("/school")
                login.assert_not_called()

    def test_persistence_keeps_session_cookies_private(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ged.Client("school", directory)
            cookie = http.cookiejar.Cookie(0, "session", "test", None, False,
                "portal.globaleduca.com", True, False, "/", True, True, None,
                True, None, None, {}, False)
            client.jar.set_cookie(cookie)
            client.save()
            restored = ged.Client("school", directory)
            self.assertEqual([c.value for c in restored.jar], ["test"])
            self.assertEqual(client.cookie_path.stat().st_mode & 0o777, 0o600)

    def test_missing_credentials_fail_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ged.Error, "GED_USERNAME"):
                ged.Client("school", directory).login()

    def test_login_posts_credentials_only_to_validated_cas_form(self):
        source = b'<form id="cas_login_form" action="https://login01.globaleduca.com/school/login?service=x"><input type="hidden" name="authenticity_token" value="csrf"></form>'
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GED_USERNAME": "test", "GED_PASSWORD": "pass"}):
            client = ged.Client("school", directory)
            with patch.object(client, "request", side_effect=[(200, "https://login02.globaleduca.com", source, "text/html"),
                   (200, ged.BASE + "/school", b"dashboard", "text/html")]) as request:
                client.login()
                sent = request.call_args_list[1].kwargs["data"]
                self.assertEqual(sent["authenticity_token"], "csrf")
                self.assertEqual(sent["username"], "test")
                self.assertEqual(sent["password"], "pass")

    def test_login_rejects_untrusted_form_without_posting_credentials(self):
        source = b'<form id="cas_login_form" action="https://other.example/login"></form>'
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GED_USERNAME": "test", "GED_PASSWORD": "pass"}):
            client = ged.Client("school", directory)
            with patch.object(client, "request", return_value=(200, "https://login02.globaleduca.com", source, "text/html")) as request:
                with self.assertRaisesRegex(ged.Error, "Unexpected"):
                    client.login()
                self.assertEqual(request.call_count, 1)


class ParserTests(unittest.TestCase):
    def test_html_preserves_text_tables_and_links_but_not_scripts_or_secrets(self):
        source = """<div id='main'><div id='header'>HEADER</div><p>Nota &amp; fecha</p>
        <script>deleteEverything()</script><table><tr><td>Materia</td><td>9</td></tr></table>
        <a href='/school/boletin.pdf'>Boletín</a><input name='email' value='a@example.org'>
        <input type='password' name='password' value='secret'>
        <input type='hidden' name='authenticity_token' value='token'></div>"""
        result = ged.page_data(source)
        self.assertEqual(result["tables"][0][0]["cells"], ["Materia", "9"])
        self.assertEqual(result["links"][0]["url"], "/school/boletin.pdf")
        self.assertEqual(result["fields"], [{"name": "email", "value": "a@example.org"}])
        self.assertNotIn("HEADER", result["text"])
        self.assertNotIn("deleteEverything", result["text"])
        self.assertNotIn("secret", str(result))

    def test_receipts_split_ranges_and_group_concepts(self):
        class Fake:
            def __init__(self):
                self.calls = []
            def json(self, suffix, params):
                self.calls.append(params)
                return {"html": """<h5 class='ttl-res'>Un recibo</h5><table>
                <tr><td>Escolaridad</td><td>01/10/2026</td><td>Pagado</td><td>50 €</td><td>Alumna</td>
                <td><a href='/school/recibos/12/comprobante'>Descargar</a></td></tr>
                <tr class='rds'><td colspan='3'>Enseñanza</td><td>50 €</td></tr></table>"""}
        client = Fake()
        args = argparse.Namespace(command="recibos", start="2025-09-01", end="2027-10-01")
        result = ged.receipts(client, args)
        self.assertEqual(len(client.calls), 3)
        for params in client.calls:
            a = ged.dt.datetime.strptime(params["fecha_inicio"], "%d/%m/%Y")
            b = ged.dt.datetime.strptime(params["fecha_fin"], "%d/%m/%Y")
            self.assertLessEqual((b - a).days, 365)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["concepts"], [{"description": "Enseñanza", "amount": "50 €"}])

    def test_virtual_file_ids_are_supported_without_remote_folder_lookup(self):
        class Fake:
            def json(self, suffix):
                self.suffix = suffix
                return {"success": True, "data": [{"id": -1, "archivos": [{"id": -4, "download_url": "/school/file"}]}]}
        client = Fake()
        args = ged.parser().parse_args(["archivos", "view", "--", "-4"])
        self.assertEqual(ged.files(client, args)["id"], -4)
        self.assertEqual(client.suffix, "archivos/estructura_carpetas.json")

    def test_json_option_before_or_after_subcommands(self):
        for command in (["--json", "horario", "list"], ["horario", "list", "--json"]):
            self.assertTrue(ged.parser().parse_args(command).json)

    def test_search_decodes_rails_fragment_without_executing_javascript(self):
        class Fake:
            def __init__(self):
                self.calls = []
            def html(self, suffix, params, ajax=False):
                self.calls.append(suffix)
                if suffix.endswith(".js"):
                    return r'''$("#resultados_busqueda").html("<table><tr class=\'show_modal\' id=\'7\' show_path=\'/school/anuncios/7\'><td>MENÚ<\/td><\/tr><tr id=\'vermas\'><td>Ver más<\/td><\/tr><\/table>");doNotExecute();'''
                return "<script>doNotExecute();</script>"
        client = Fake()
        args = ged.parser().parse_args("anuncios search MENÚ --desde 2026-09-01 --hasta 2026-12-31".split())
        result = ged.search_announcements(client, args)
        self.assertTrue(result["complete"])
        self.assertEqual(result["entries"][0]["id"], "7")
        self.assertNotIn("doNotExecute", result["text"])
        self.assertEqual(client.calls, ["anuncios/search.js", "anuncios"])

    def test_download_does_not_overwrite_existing_file(self):
        class Fake:
            def section(self, suffix, params):
                return (200, ged.BASE + "/school/document", b"document", "application/octet-stream")
        with tempfile.TemporaryDirectory() as directory:
            path = ged.Path(directory) / "document"
            path.write_bytes(b"original")
            args = argparse.Namespace(command="recibos", id=12, output=str(path))
            with self.assertRaisesRegex(ged.Error, "already exists"):
                ged.download(Fake(), args)
            self.assertEqual(path.read_bytes(), b"original")

    def test_invalid_dates_are_rejected(self):
        with self.assertRaises(ged.Error):
            ged.dates(argparse.Namespace(command="horario", start="2026-10-20", end="2026-10-01"))


if __name__ == "__main__":
    unittest.main()
