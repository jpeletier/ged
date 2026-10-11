#!/usr/bin/env python3
"""GlobalEduca CLI. Python 3.10+, standard library only."""

import argparse
import contextlib
import datetime as dt
import html.parser
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
BASE = "https://portal.globaleduca.com"
CALENDARS = {"horario": "horarios/find_general", "ausencias": "ausencias/find",
             "incidencias": "incidencias/find", "entrevistas": "entrevistas/find",
             "extraescolares": "extraescolares/find"}
PAGES = {"boletines": "notas/boletines", "parciales": "notas/parciales",
         "autorizaciones": "autorizaciones", "perfil": "ajustes"}


class Error(Exception):
    pass


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self, tag=None, cls=None, ident=None):
        for child in self.children:
            if isinstance(child, Node):
                if ((tag is None or child.tag == tag)
                        and (cls is None or cls in (child.attrs.get("class") or "").split())
                        and (ident is None or child.attrs.get("id") == ident)):
                    yield child
                yield from child.walk(tag, cls, ident)

    def first(self, **kw):
        return next(self.walk(**kw), None)

    def text(self):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return " ".join(" ".join(
            c.text() if isinstance(c, Node) else c for c in self.children).split())


class DOM(html.parser.HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def text(node):
    return node.text() if node is not None else ""


def document(source):
    root = DOM(source).root
    main = root.first(ident="main") or root
    def clean(node):
        node.children = [c for c in node.children if not isinstance(c, Node) or (
            c.attrs.get("id") not in {"header", "mimodal", "mimodalgrande"}
            and "toast" not in (c.attrs.get("class") or "").split())]
        for c in node.children:
            if isinstance(c, Node):
                clean(c)
    clean(main)
    return main


def links(node):
    return [{"text": a.text(), "url": a.attrs["href"]}
            for a in node.walk(tag="a") if a.attrs.get("href", "").startswith("/")]


def page_data(source):
    root = document(source)
    tables = []
    for table in root.walk(tag="table"):
        rows = []
        for row in table.walk(tag="tr"):
            cells = [c.text() for c in row.children if isinstance(c, Node)
                     and c.tag in {"th", "td"}]
            if cells:
                rows.append({"cells": cells, "links": links(row)})
        if rows:
            tables.append(rows)
    entries = [{"id": n.attrs.get("id"), "url": n.attrs["show_path"], "text": n.text()}
               for n in root.walk() if n.attrs.get("show_path")]
    fields = []
    for n in root.walk(tag="input"):
        name = n.attrs.get("name", "")
        if (name and n.attrs.get("type") not in {"hidden", "password", "submit", "file"}
                and not re.search("password|token|secret", name, re.I)):
            fields.append({"name": name, "value": n.attrs.get("value", "")})
    for n in root.walk(tag="select"):
        fields.append({"name": n.attrs.get("name", ""),
                       "options": [{"value": o.attrs.get("value"), "text": o.text(),
                                    "selected": "selected" in o.attrs}
                                   for o in n.walk(tag="option")]})
    return {"text": root.text(), "tables": tables, "entries": entries,
            "links": links(root), "fields": fields}


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        # CAS emits some HTTP Location headers; never send cookies or tickets without TLS.
        if url.scheme == "http" and (url.hostname or "").endswith(".globaleduca.com"):
            newurl = urllib.parse.urlunsplit(url._replace(scheme="https"))
            url = urllib.parse.urlsplit(newurl)
        if (url.scheme != "https" or not url.hostname or
                not (url.hostname == "globaleduca.com" or url.hostname.endswith(".globaleduca.com"))):
            raise Error(f"Unexpected redirect: {url.scheme}://{url.hostname}{url.path}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, centro, state_dir, timeout=30):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", centro):
            raise Error("Invalid school identifier.")
        self.centro = centro
        self.prefix = f"/{centro}"
        self.timeout = timeout
        self.directory = Path(state_dir).expanduser() / centro
        self.cookie_path = self.directory / "cookies.lwp"
        self.jar = http.cookiejar.LWPCookieJar(str(self.cookie_path))
        if self.cookie_path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                self.jar.clear()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), Redirects())
        self.opener.addheaders = [("User-Agent", f"ged/{VERSION} (Python stdlib)"),
                                  ("Accept", "text/html,application/json")]

    def save(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        fd, name = tempfile.mkstemp(prefix=".cookies-", dir=self.directory)
        os.close(fd)
        try:
            self.jar.save(name, ignore_discard=True, ignore_expires=True)
            os.replace(name, self.cookie_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def request(self, path, params=None, data=None, ajax=False):
        url = urllib.parse.urljoin(BASE, path)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or
                not parsed.hostname.endswith(".globaleduca.com")):
            raise Error("URL is outside GlobalEduca.")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {"X-Requested-With": "XMLHttpRequest"} if ajax else {}
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                result = (response.status, response.geturl(), response.read(),
                          response.headers.get_content_type())
        except urllib.error.HTTPError as exc:
            result = (exc.code, exc.geturl(), exc.read(), exc.headers.get_content_type())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Error(f"Could not connect to the portal: {exc}") from None
        return result

    @staticmethod
    def expired(response):
        status, url, body, _ = response
        return status == 401 or "login" in (urllib.parse.urlsplit(url).hostname or "") or b'cas_login_form' in body

    def login(self):
        user = os.environ.get("GED_USERNAME") or os.environ.get("GED_USER")
        password = os.environ.get("GED_PASSWORD") or os.environ.get("GED_PASS")
        if not user or not password:
            raise Error("No session available. Set GED_USERNAME and GED_PASSWORD to sign in.")
        # Only authentication submits POST; information collectors only call get().
        status, _, body, _ = self.request(self.prefix)
        if status != 200:
            raise Error(f"HTTP {status} while fetching the login form.")
        root = DOM(body.decode("utf-8", "replace")).root
        form = root.first(tag="form", ident="cas_login_form")
        if form is None:
            raise Error("Unrecognized CAS login form.")
        action = form.attrs.get("action", "")
        parsed = urllib.parse.urlsplit(action)
        if (parsed.scheme != "https" or not re.fullmatch(r"login\d+\.globaleduca\.com", parsed.hostname or "")
                or parsed.path != f"{self.prefix}/login"):
            raise Error("Unexpected CAS authentication destination.")
        hidden = {n.attrs.get("name"): n.attrs.get("value", "")
                  for n in form.walk(tag="input") if n.attrs.get("type") == "hidden"}
        hidden.update(username=user, password=password, commit="Iniciar sesión")
        result = self.request(action, data=hidden)
        if result[0] != 200 or self.expired(result):
            raise Error("Sign-in failed. Check your credentials or the portal's availability.")
        if urllib.parse.urlsplit(result[1]).hostname != "portal.globaleduca.com":
            raise Error("Authentication did not return to the portal.")
        self.save()

    def get(self, path, params=None, ajax=False):
        result = self.request(path, params=params, ajax=ajax)
        if self.expired(result):
            self.login()
            result = self.request(path, params=params, ajax=ajax)
        if self.expired(result):
            raise Error("The session is still invalid after signing in again.")
        if result[0] != 200:
            raise Error(f"HTTP {result[0]} while querying {path}.")
        self.save()
        return result

    def section(self, suffix="", params=None, ajax=False):
        return self.get(self.prefix + ("/" + suffix if suffix else ""), params, ajax)

    def html(self, suffix="", params=None, ajax=False):
        return self.section(suffix, params, ajax)[2].decode("utf-8", "replace")

    def json(self, suffix, params=None):
        result = self.section(suffix, params, ajax=True)
        try:
            return json.loads(result[2])
        except ValueError:
            raise Error(f"Invalid JSON response from {suffix}.") from None


def dates(args):
    today = dt.date.today()
    year = today.year if today.month >= 9 else today.year - 1
    start = args.start or (today.isoformat() if args.command == "horario" else f"{year}-09-01")
    end = args.end or ((today + dt.timedelta(days=7)).isoformat()
                       if args.command == "horario" else f"{year + 1}-09-01")
    try:
        a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise Error("Dates must use YYYY-MM-DD.") from None
    if a >= b:
        raise Error("--desde must precede --hasta (the end date is exclusive for calendars).")
    return a, b


def summary(client):
    root = DOM(client.html()).root
    cards = []
    for card in root.walk(cls="bh"):
        counts = [{"label": text(s.first(cls="top")), "value": text(s.first(cls="total"))}
                  for s in card.walk(cls="stt")]
        items = [{"id": n.attrs.get("id"), "text": n.text(), "url": n.attrs["show_path"]}
                 for n in card.walk() if n.attrs.get("show_path")]
        cards.append({"section": text(card.first(cls="navtabs")), "counts": counts, "items": items})
    return {"academic_year": text(root.first(cls="ttl_seccion")),
            "student": text(root.first(cls="alshader")), "cards": cards}


def announcements(client, args):
    items, seen = [], set()
    for page in range(args.max_pages):
        params = {"tab": "publicados"}
        if page:
            params.update(pagina=page, render_partial="true")
        root = DOM(client.html("anuncios", params, ajax=bool(page))).root
        count = 0
        for row in root.walk(tag="tr", cls="show_modal"):
            ident = row.attrs.get("id")
            if ident in seen:
                continue
            seen.add(ident)
            cells = list(row.walk(tag="td"))
            items.append({"id": ident, "title": text(cells[0]) if cells else row.text(),
                          "date": text(cells[1]) if len(cells) > 1 else "",
                          "attachments": text(cells[2]) if len(cells) > 2 else "",
                          "unread": row.first(cls="objeto_no_leido") is not None,
                          "url": row.attrs.get("show_path")})
            count += 1
        if not count or root.first(ident="vermas") is None:
            return {"items": items, "complete": True}
    return {"items": items, "complete": False, "reason": "Reached the --max-pages limit"}


def search_announcements(client, args):
    a, b = dates(args)
    source = client.html("anuncios/search.js", {"desde": a.strftime("%d/%m/%Y"),
        "hasta": b.strftime("%d/%m/%Y"), "asunto": args.query, "tipo": "publicados", "commit": "Buscar"}, ajax=True)
    # Rails UJS wraps HTML in a JS string. Decode that string;
    # never evaluate or execute the server response.
    match = re.search(r'\.html\("((?:\\.|[^"\\])*)"\)', source)
    if not match:
        raise Error("Unexpected search format: the HTML fragment was not recognized.")
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    def unescape(m):
        value = m[1]
        return chr(int(value[1:], 16)) if value.startswith(("u", "x")) and len(value) > 1 else escapes.get(value, value)
    fragment = re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", unescape, match[1])
    root = DOM(fragment).root
    complete = root.first(ident="vermas") is None
    pages = [fragment]
    seen = {n.attrs.get("id") for n in root.walk(tag="tr", cls="show_modal")}
    # Follow exactly the AJAX 'load more' request returned by the search interface.
    for page in range(1, args.max_pages):
        if complete:
            break
        partial = client.html("anuncios", {"tab": "publicados", "pagina": page,
                                              "render_partial": "true"}, ajax=True)
        more = DOM(partial).root
        identifiers = {n.attrs.get("id") for n in more.walk(tag="tr", cls="show_modal")}
        if not identifiers:
            complete = True
            break
        if not identifiers - seen:
            break
        pages.append(partial)
        seen.update(identifiers)
        complete = more.first(ident="vermas") is None
    fragment = "\n".join(pages)
    data = page_data(fragment)
    data["complete"] = complete
    if not complete:
        data["limitation"] = "Page limit or repeated server results; query incomplete."
    return fragment if args.html else data


def calendar(client, args):
    a, b = dates(args)
    data = client.json(CALENDARS[args.command], {"start": a.isoformat(), "end": b.isoformat()})
    if not isinstance(data, list):
        raise Error("The calendar did not return a list of events.")
    return {"start": a.isoformat(), "end_exclusive": b.isoformat(),
            "events": sorted(data, key=lambda e: (e.get("start", ""), e.get("title", "")))}


def receipts(client, args):
    a, b = dates(args)
    parts, items, seen = [], [], set()
    while a <= b:
        stop = min(a + dt.timedelta(days=365), b)
        value = client.json("recibos/find", {"fecha_inicio": a.strftime("%d/%m/%Y"),
                                             "fecha_fin": stop.strftime("%d/%m/%Y"), "commit": "Buscar"})
        source = value.get("html") if isinstance(value, dict) else None
        if not isinstance(source, str):
            raise Error("Unexpected receipt format: missing html.")
        root = DOM(source).root
        parts.append(text(root.first(cls="ttl-res")))
        current = None
        for row in root.walk(tag="tr"):
            cells = [c for c in row.children if isinstance(c, Node) and c.tag == "td"]
            if not cells:
                continue
            if "rds" in (row.attrs.get("class") or "").split():
                if current:
                    current["concepts"].append({"description": text(cells[0]),
                                                 "amount": text(cells[1]) if len(cells) > 1 else ""})
            elif len(cells) >= 5:
                dl = next((l["url"] for l in links(row) if "/comprobante" in l["url"]), None)
                match = re.search(r"/recibos/(\d+)/", dl or "")
                current = {"id": match[1] if match else None, "name": text(cells[0]),
                           "date": text(cells[1]), "status": text(cells[2]), "amount": text(cells[3]),
                           "student": text(cells[4]), "download_url": dl, "concepts": []}
                key = current["id"] or (current["name"], current["date"])
                if key not in seen:
                    items.append(current)
                    seen.add(key)
        if "no es correcto" in source or "debe estar entre" in source:
            raise Error(text(root.first(cls="ttl-res")))
        a = stop + dt.timedelta(days=1)
    return {"items": items, "messages": parts}


def files(client, args):
    tree = client.json("archivos/estructura_carpetas.json")
    if not isinstance(tree, dict) or tree.get("success") is not True:
        raise Error("Could not retrieve the folder structure.")
    if args.id is None:
        return tree
    found = next((n for n in file_nodes(tree.get("data", [])) if n.get("id") == args.id), None)
    if found is not None:
        return found
    if args.id > 0:
        return client.json(f"archivos/carpeta/{args.id}.json")
    raise Error(f"File or folder ID not found: {args.id}")


def file_nodes(nodes):
    for node in nodes:
        yield node
        yield from file_nodes(node.get("subcarpetas", []))
        yield from file_nodes(node.get("archivos", []))


def detail(client, args):
    section = {"horario": "horarios"}.get(args.command, args.command)
    suffix = f"{section}/{args.id}"
    if args.command == "entrevistas":
        suffix += "/edit"
    params = {"modal": "true"}
    if getattr(args, "date", None):
        params["fecha"] = args.date
    source = client.html(suffix, params, ajax=True)
    return source if args.html else page_data(source)


def download(client, args):
    if args.command == "anuncios":
        suffix, params = f"anuncios/{args.id}/adjunto", {"id": args.attachment}
    elif args.command == "recibos":
        suffix, params = f"recibos/{args.id}/comprobante", None
    elif args.command == "archivos":
        node = files(client, args)
        url = node.get("download_url")
        if not url or not url.startswith(client.prefix + "/archivos/"):
            raise Error("The ID does not identify a downloadable document.")
        suffix, params = url.removeprefix(client.prefix + "/"), None
    elif args.command in {"boletines", "parciales", "autorizaciones"}:
        data = page_data(client.html(PAGES[args.command]))
        allowed = {l["url"] for l in data["links"] if re.search(r"comprobante|adjunto|download|descarga|\.pdf|boletin", l["url"], re.I)
                   and not re.search(r"/ajustes|/new|/edit|/publish|/delete|/firmar|/aceptar", l["url"])}
        if args.url not in allowed or args.url == client.prefix + "/" + PAGES[args.command]:
            raise Error("The URL is not a document linked from this section.")
        suffix, params = args.url.removeprefix(client.prefix + "/"), None
    else:
        raise Error("Downloads are not available in this section.")
    result = client.section(suffix, params)
    if result[3] in {"text/html", "application/json"}:
        raise Error("The portal did not return a downloadable document.")
    path = Path(args.output).expanduser()
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target:
            target.write(result[2])
    except FileExistsError:
        raise Error(f"File already exists: {path}") from None
    return {"file": str(path), "bytes": len(result[2]), "content_type": result[3]}


def run(client, args):
    if args.command == "auth":
        if args.action == "clear":
            if client.cookie_path.exists():
                client.cookie_path.unlink()
            return {"local_cookie_removed": True}
        if args.action == "login":
            client.get(client.prefix)
            return {"authenticated": True, "cookie_file": str(client.cookie_path)}
        result = client.request(client.prefix)
        ok = result[0] == 200 and not client.expired(result)
        if result[0] not in {200, 401}:
            raise Error(f"HTTP {result[0]} while checking the session.")
        if ok:
            client.save()
        return {"authenticated": ok, "cookie_file": str(client.cookie_path)}
    if args.command == "resumen":
        return summary(client)
    if args.command == "snapshot":
        result, errors = {}, {}
        for section in ["resumen", "anuncios", *CALENDARS, *PAGES, "recibos", "archivos", "alumnos"]:
            child = argparse.Namespace(**vars(args))
            child.command, child.action = section, "list"
            child.id, child.html, child.past, child.max_pages, child.area = None, False, False, 100, "personal"
            try:
                result[section] = run(client, child)
            except Error as exc:
                errors[section] = str(exc)
        limitations = []
        if "alumnos" in result and not result["alumnos"].get("complete", True):
            limitations.append("Student selector unavailable; querying the student in the current session.")
        if "anuncios" in result and not result["anuncios"].get("complete", True):
            limitations.append("Announcement pagination incomplete.")
        for name, suffix in [("autorizaciones_pasadas", "autorizaciones/pasadas"),
                             ("perfil_notas", "ajustes/notas"), ("perfil_notificaciones", "ajustes/notificaciones")]:
            try:
                result[name] = page_data(client.html(suffix))
            except Error as exc:
                errors[name] = str(exc)
        search_args = argparse.Namespace(**vars(args))
        search_args.command, search_args.action = "anuncios", "search"
        search_args.query, search_args.max_pages, search_args.html = "", 100, False
        try:
            result["anuncios_busqueda"] = search_announcements(client, search_args)
            if not result["anuncios_busqueda"]["complete"]:
                limitations.append("Announcement search incomplete.")
        except Error as exc:
            errors["anuncios_busqueda"] = str(exc)
        return {"sections": result, "errors": errors, "limitations": limitations,
                "complete": not errors and not limitations}
    if args.action == "download":
        return download(client, args)
    if args.action == "view" and args.command not in {"perfil", "boletines", "parciales"}:
        if args.command == "archivos":
            return files(client, args)
        return detail(client, args)
    if args.command == "anuncios":
        if args.action == "search":
            return search_announcements(client, args)
        return announcements(client, args)
    if args.command in CALENDARS:
        return calendar(client, args)
    if args.command == "recibos":
        return receipts(client, args)
    if args.command == "archivos":
        return files(client, args)
    if args.command == "alumnos":
        try:
            source = client.html("alumnos", {"modal": "true"}, ajax=True)
            return {"complete": True, **page_data(source)}
        except Error as exc:
            if "HTTP 500" not in str(exc):
                raise
            root = DOM(client.html()).root
            students = [{"name": n.text()} for n in root.walk(cls="alshader")]
            return {"complete": False, "source": "dashboard", "students": students,
                    "limitation": "The /alumnos selector returns HTTP 500; only the visible student is identified."}
    else:
        suffix = PAGES[args.command]
        if args.command == "autorizaciones" and args.past:
            suffix += "/pasadas"
        if args.command == "perfil" and args.area != "personal":
            suffix += "/" + {"notas": "notas", "notificaciones": "notificaciones"}[args.area]
        source = client.html(suffix)
    return source if args.html else page_data(source)


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return n


def output_flags(parser):
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Structured JSON output for agents and scripts")
    parser.add_argument("--html", action="store_true", default=False,
                        help="Original HTML for HTML-based sections (without executing JavaScript)")


def date_flags(parser):
    parser.add_argument("--desde", dest="start", metavar="YYYY-MM-DD", help="Start of the range")
    parser.add_argument("--hasta", dest="end", metavar="YYYY-MM-DD", help="End of the range (exclusive for calendars, inclusive for receipts)")


def parser():
    p = argparse.ArgumentParser(prog="ged", description="Query GlobalEduca from the terminal (standard library only).",
        epilog="Required school: GED_CENTRO. Credentials: GED_USERNAME and GED_PASSWORD. Cookies are reused automatically. Example: ged horario list --json. Opening details may mark them as read on the server.")
    p.add_argument("--version", action="version", version=f"ged {VERSION}")
    p.set_defaults(centro=os.environ.get("GED_CENTRO"))
    p.add_argument("--state-dir", default=os.environ.get("GED_STATE_DIR", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ged")),
                   help="Local cookie directory (GED_STATE_DIR)")
    p.add_argument("--timeout", type=positive, default=30, help="HTTP timeout in seconds")
    p.add_argument("--json", action="store_true", help="JSON output")
    sub = p.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Authentication and local session cookies")
    acts = auth.add_subparsers(dest="action", required=True)
    for name, desc in [("status", "Check cookies without submitting credentials"), ("login", "Reuse cookies or sign in"),
                       ("clear", "Remove local cookies without signing out remotely")]:
        output_flags(acts.add_parser(name, help=desc))
    for name, desc in [("resumen", "Portal dashboard and counters"), ("snapshot", "Query all sections and report partial failures")]:
        q = sub.add_parser(name, help=desc)
        output_flags(q)
        if name == "snapshot":
            date_flags(q)
    descriptions = {"anuncios": "Announcements and attachments", "horario": "Class schedules and holidays",
                    "ausencias": "Attendance and absences", "incidencias": "Pedagogical observations",
                    "entrevistas": "Meetings with teachers", "extraescolares": "Activities and services",
                    "boletines": "Report cards", "parciales": "Interim grades",
                    "recibos": "Receipts and payment confirmations", "autorizaciones": "Current and past authorizations",
                    "archivos": "Shared folders and documents", "perfil": "Account information and preferences",
                    "alumnos": "Students available in the session"}
    descriptions["festivos"] = "Holiday details linked from the schedule"
    for name, desc in descriptions.items():
        section = sub.add_parser(name, help=desc, description=desc)
        commands = section.add_subparsers(dest="action", required=True)
        if name == "festivos":
            q = commands.add_parser("view", help="View a holiday by the ID in its schedule link")
            q.add_argument("id", type=positive)
            output_flags(q)
            continue
        q = commands.add_parser("list" if name != "perfil" else "view", help="Query information")
        output_flags(q)
        if name in CALENDARS or name == "recibos":
            date_flags(q)
        if name == "anuncios":
            q.add_argument("--max-pages", type=positive, default=100, help="Maximum pages (100); complete=false if reached")
            search = commands.add_parser("search", help="Search announcements by subject and dates")
            search.add_argument("query", nargs="?", default="", help="Subject text")
            search.add_argument("--max-pages", type=positive, default=100, help="Maximum result pages")
            date_flags(search)
            output_flags(search)
        if name == "autorizaciones":
            q.add_argument("--pasadas", dest="past", action="store_true", help="Query past authorizations")
        if name == "perfil":
            q.add_argument("--area", choices=["personal", "notas", "notificaciones"], default="personal")
        if name == "archivos":
            q.set_defaults(id=None)
        if name in {"anuncios", *CALENDARS, "autorizaciones", "archivos"}:
            view = commands.add_parser("view", help="View details by ID (may mark them as read on the server)")
            view.add_argument("id", type=int if name == "archivos" else positive)
            output_flags(view)
            if name == "horario":
                view.add_argument("--fecha", dest="date", metavar="YYYY-MM-DD", help="Class date as shown in the event")
        if name in {"anuncios", "recibos", "archivos", "boletines", "parciales", "autorizaciones"}:
            dl = commands.add_parser("download", help="Download an attachment or document")
            if name in {"boletines", "parciales", "autorizaciones"}:
                dl.add_argument("--url", required=True, help="Relative document URL returned by list")
            else:
                dl.add_argument("id", type=int if name == "archivos" else positive)
            if name == "anuncios":
                dl.add_argument("--adjunto", dest="attachment", type=positive, required=True, help="Attachment ID returned by view")
            dl.add_argument("--output", required=True, help="Destination file; existing files are never overwritten")
            output_flags(dl)
    return p


def show(value, indent=0):
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{pad}{key}:")
                show(item, indent + 2)
            elif item is not None and item != "":
                print(f"{pad}{key}: {item}")
    elif isinstance(value, list):
        if not value:
            print(pad + "(no items)")
        for item in value:
            print(pad + "—")
            show(item, indent + 2)
    else:
        print(pad + str(value))


def table(headers, rows):
    if not rows:
        print("No items.")
        return
    widths = [min(60, max(len(str(h)), *(len(str(r[i] if r[i] is not None else "")) for r in rows)))
              for i, h in enumerate(headers)]
    print("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("─" * w for w in widths))
    for row in rows:
        cells = [textwrap.wrap(str(v if v is not None else ""), width=w) or [""] for v, w in zip(row, widths)]
        for line in range(max(map(len, cells))):
            print("  ".join((cell[line] if line < len(cell) else "").ljust(w)
                            for cell, w in zip(cells, widths)))


def human(args, data):
    if isinstance(data, str):
        print(data)
    elif isinstance(data, dict) and "events" in data:
        print(f"Range: {data['start']} → {data['end_exclusive']} (exclusive end)")
        days = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
                "4": "Thursday", "5": "Friday", "6": "Saturday"}
        rows = []
        for e in data["events"]:
            start = e.get("start", "")
            dow = e.get("dow")
            if dow is not None:
                values = dow if isinstance(dow, list) else [dow]
                day = ", ".join(days.get(str(v), str(v)) for v in values) + " (recurring)"
            else:
                day = start[:10] if len(start) > 10 else e.get("date", "")
            hour = "all day" if e.get("allDay") else (start[11:] if len(start) > 10 else start)
            end = e.get("end", "")
            if end:
                hour += "–" + (end[11:] if len(end) > 10 else end)
            rows.append([e.get("id"), day, hour, e.get("title")])
        table(["ID", "Day", "Time", "Activity"], rows)
    elif args.command == "anuncios" and args.action == "list":
        table(["ID", "Title", "Dates", "Attachments", "New"],
              [[e["id"], e["title"], e["date"], e["attachments"], "yes" if e["unread"] else "no"]
               for e in data["items"]])
        if not data["complete"]:
            print(data["reason"])
    elif args.command == "recibos" and args.action == "list":
        table(["ID", "Receipt", "Date", "Status", "Amount"],
              [[e["id"], e["name"], e["date"], e["status"], e["amount"]] for e in data["items"]])
        for e in data["items"]:
            print(f"\nLine items for {e['id']}:")
            table(["Description", "Amount"], [[c["description"], c["amount"]] for c in e["concepts"]])
    elif args.command == "resumen":
        print(data["academic_year"] + " — " + data["student"])
        table(["Section", "Counters"], [[c["section"], "; ".join(
            f"{n['label']}: {n['value']}" for n in c["counts"])] for c in data["cards"]])
        for card in data["cards"]:
            for item in card["items"]:
                print(f"\n{card['section']}: {item['text']}")
    else:
        show(data)


@contextlib.contextmanager
def state_lock(client):
    """Serialize cookie access between cron or agent invocations on Unix."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    client.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(client.directory / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Another invocation may have renewed cookies while we waited for the lock.
        if client.cookie_path.exists():
            client.jar.clear()
            try:
                client.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                client.jar.clear()
        yield
    finally:
        os.close(fd)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.json and getattr(args, "html", False):
            raise Error("--json and --html cannot be combined.")
        if not args.centro:
            raise Error("GED_CENTRO is required. Set it to your school's portal identifier.")
        client = Client(args.centro, args.state_dir, args.timeout)
        with state_lock(client):
            data = run(client, args)
        result = {"schema_version": 1, "center": args.centro,
                  "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "command": args.command, "data": data}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            human(args, data)
        if args.command == "auth" and args.action == "status" and not data["authenticated"]:
            return 1
        if args.command in {"snapshot", "alumnos", "anuncios"} and isinstance(data, dict) and not data.get("complete", True):
            return 1
        return 0
    except Error as exc:
        if args.json:
            print(json.dumps({"schema_version": 1, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ged: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        message = f"File or connection error: {exc}"
        if args.json:
            print(json.dumps({"schema_version": 1, "error": message}, ensure_ascii=False))
        else:
            print(f"ged: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
