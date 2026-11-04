"""Read public product-page facts into an unsaved, reviewable coffee profile.

Remote HTML is data only: no scripts, images, linked resources or instructions
are executed. Connections use validated, pinned public addresses, not a second
hostname lookup by the HTTP client.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date
from html import unescape
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, unquote_plus, urljoin, urlsplit, urlunsplit

from .models import Profile


MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TOTAL_TIMEOUT = 20
IO_TIMEOUT = 6
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="product-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)


class ProductLookupError(ValueError):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProductLookupError("The product page took too long to respond. Try again later.", 504)
    return remaining


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ProductLookupError("The product address could not be validated.") from exc
    if not address.is_global or address.is_reserved or address.is_multicast:
        raise ProductLookupError("Only public internet product pages can be fetched; local and reserved addresses are blocked.")
    if address.version == 6 and (address.ipv4_mapped or address.sixtofour or address.teredo
            or address in ipaddress.ip_network("64:ff9b::/96")
            or address in ipaddress.ip_network("64:ff9b:1::/48")):
        raise ProductLookupError("Translated or embedded IP addresses are not supported.")
    return address


def _without_tracking(query):
    parts = []
    for part in query.split("&"):
        key = unquote_plus(part.split("=", 1)[0]).casefold()
        if part and key not in {"shop_consented_scopes", "shop_sign_in"} and not key.startswith("utm_"):
            parts.append(part)
    return "&".join(parts)


def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ProductLookupError("Enter a public HTTPS product URL (maximum 2048 characters).")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ProductLookupError("The product URL contains invalid characters.")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.port not in {None, 443}:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if "%" in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if len(host) > 253 or len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
                raise ValueError
        else:
            _public_address(address)
            host = str(address)
        authority = f"[{host}]" if ":" in host else host
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(_without_tracking(parts.query), safe="/%?:@!$&'()*+,;=-._~")
        return urlunsplit(("https", authority, path, query, ""))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ProductLookupError):
            raise
        raise ProductLookupError("Use HTTPS on port 443, without a username, password, or local hostname.") from exc


def _resolve_public(host, deadline):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [_public_address(address)]
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ProductLookupError("Product lookup is busy. Try again shortly.", 503)
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    except Exception:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        answers = future.result(timeout=min(IO_TIMEOUT, _remaining(deadline)))
    except FutureTimeout as exc:
        future.cancel()
        raise ProductLookupError("The product hostname took too long to resolve.", 504) from exc
    except OSError as exc:
        raise ProductLookupError("The product hostname could not be resolved.", 502) from exc
    if not answers or len(answers) > 32:
        raise ProductLookupError("The product hostname returned an unsupported address list.")
    # Reject mixed public/private answers rather than falling back to a public one.
    addresses = {_public_address(answer[4][0]) for answer in answers}
    return sorted(addresses, key=lambda address: (address.version, str(address)))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, deadline):
        super().__init__(host, port=443, timeout=min(IO_TIMEOUT, _remaining(deadline)), context=ssl.create_default_context())
        self.address = address
        self.deadline = deadline
        self.transport_socket = None

    def connect(self):
        # Do not call HTTPSConnection.connect: it would resolve the hostname again.
        family = socket.AF_INET6 if self.address.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        self.transport_socket = self.sock = sock
        try:
            sock.settimeout(min(IO_TIMEOUT, _remaining(self.deadline)))
            target = (str(self.address), 443, 0, 0) if family == socket.AF_INET6 else (str(self.address), 443)
            sock.connect(target)
            sock.settimeout(min(IO_TIMEOUT, _remaining(self.deadline)))
            self.transport_socket = self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise

    def abort(self):
        sock = self.transport_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def _fetch_hop(url, address, deadline):
    parts = urlsplit(url)
    connection = _PinnedHTTPSConnection(parts.hostname, address, deadline)
    watchdog = threading.Timer(_remaining(deadline), connection.abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection.request("GET", target, headers={
            "Host": parts.netloc, "User-Agent": "xBloom-Local/1.0 product-profile-preview",
            "Accept": "text/html, application/xhtml+xml", "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        _remaining(deadline)
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location")
            if not location:
                raise ProductLookupError("The product page returned an empty redirect.", 502)
            return None, location
        if response.status != 200:
            raise ProductLookupError(f"The product page returned HTTP {response.status}. Check the URL or try again later.", 502)
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ProductLookupError("That URL did not return an HTML product page.")
        if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ProductLookupError("The website returned an unsupported compressed response.")
        length = response.getheader("Content-Length")
        if length:
            try:
                if not 0 <= int(length) <= MAX_BYTES:
                    raise ValueError
            except ValueError as exc:
                raise ProductLookupError("The product page is larger than the 2 MB lookup limit.") from exc
        chunks, size = [], 0
        while True:
            remaining = _remaining(deadline)
            if connection.transport_socket is not None:
                connection.transport_socket.settimeout(min(IO_TIMEOUT, remaining))
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ProductLookupError("The product page is larger than the 2 MB lookup limit.")
            chunks.append(chunk)
        _remaining(deadline)
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            html = b"".join(chunks).decode(charset, errors="replace")
        except LookupError:
            html = b"".join(chunks).decode("utf-8", errors="replace")
        return html, None
    except ProductLookupError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise ProductLookupError("The product page took too long to respond. Try again later.", 504) from exc
    except (OSError, http.client.HTTPException) as exc:
        _remaining(deadline)
        raise ProductLookupError("The product page could not be securely fetched. Check the URL or try again later.", 502) from exc
    finally:
        watchdog.cancel()
        connection.close()


def fetch_page(url):
    deadline = time.monotonic() + TOTAL_TIMEOUT
    current = validate_url(url)
    visited = set()
    for hop in range(MAX_REDIRECTS + 1):
        if current in visited:
            raise ProductLookupError("The product page redirects in a loop.")
        visited.add(current)
        addresses = _resolve_public(urlsplit(current).hostname, deadline)
        html, redirect = _fetch_hop(current, addresses[0], deadline)
        if redirect is None:
            return html, current
        if hop == MAX_REDIRECTS:
            raise ProductLookupError("The product page has too many redirects.")
        # Every hop gets new URL validation, DNS validation and a pinned connection.
        current = validate_url(urljoin(current, redirect))
    raise ProductLookupError("The product page could not be read.", 502)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.script = None
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.skip += 1
            if attributes.get("type", "").split(";", 1)[0].lower() == "application/ld+json":
                self.script = []
        elif tag in {"style", "noscript", "template"}:
            self.skip += 1
        elif not self.skip and tag in {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "dt", "dd"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script":
            if self.script is not None:
                self.scripts.append("".join(self.script))
                self.script = None
            self.skip = max(0, self.skip - 1)
        elif tag in {"style", "noscript", "template"}:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "dt", "dd"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif not self.skip:
            self.parts.append(data)


def _plain(value, limit=10000):
    if not isinstance(value, str):
        return ""
    parser = _PageParser()
    try:
        parser.feed(value[:limit])
    except (ValueError, AssertionError):
        return ""
    return unescape("".join(parser.parts)).strip()


def _products(scripts):
    products = []
    for script in scripts[:40]:
        try:
            value = json.loads(script)
        except (ValueError, RecursionError):
            continue
        pending, seen = [value], 0
        while pending and seen < 10000:
            node = pending.pop()
            seen += 1
            if isinstance(node, dict):
                types = node.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if isinstance(types, list) and any(str(kind).rsplit("/", 1)[-1] == "Product" for kind in types):
                    products.append(node)
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
    return products


_LABELS = {
    "country": "country", "region": "region", "farm": "farm", "estate": "farm",
    "elevation": "elevation", "altitude": "elevation", "process": "process", "processing": "process",
    "variety": "variety", "varieties": "variety", "varietal": "variety", "cultivar": "variety",
    "roast level": "roast_level", "roast date": "roast_date", "roasted on": "roast_date",
    "tastes like": "flavor_notes", "tasting notes": "flavor_notes", "flavor notes": "flavor_notes",
    "flavour notes": "flavor_notes", "producer": "producer", "origin": "origin", "roaster": "roaster",
}
_COUNTRIES = {name.casefold() for name in (
    "Brazil", "Colombia", "Ethiopia", "Kenya", "Panama", "Costa Rica", "Guatemala", "Honduras",
    "El Salvador", "Nicaragua", "Mexico", "Peru", "Ecuador", "Bolivia", "Rwanda", "Burundi",
    "Uganda", "Tanzania", "Yemen", "Indonesia", "India", "China", "Vietnam", "Thailand",
    "Laos", "Papua New Guinea", "Timor-Leste", "Philippines", "United States", "Jamaica",
    "Dominican Republic", "Haiti", "Cuba", "Venezuela", "Democratic Republic of the Congo",
)}
_EMBEDDED_LABEL = re.compile(r"(?:^|\s)(?:" + "|".join(re.escape(label) for label in _LABELS) + r")\s*:", re.I)


def _labeled_facts(text):
    facts = {}
    for line in text.splitlines():
        line = " ".join(line.split()).strip()
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        field = _LABELS.get(label.strip().casefold())
        value = value.strip()
        if field and value and len(value) <= 500 and field not in facts and not _EMBEDDED_LABEL.search(value):
            facts[field] = value
    return facts


def recommended_recipe(bean):
    fermented = bool(re.search(r"ferment|anaerobic|thermal.shock", bean.get("process") or "", re.I))
    rationale = ("The reported fermentation process suggests starting gently: descending temperatures and no added agitation aim to preserve clarity."
                 if fermented else "This moderate starting recipe uses descending temperatures and no added agitation; adjust after tasting.")
    return {
        "name": ("18 g starting recipe · " + bean["name"])[:160], "dose_g": 18, "ratio": 16,
        "total_water_ml": 288, "grind_setting": 55, "grinder_rpm": 80, "grinder_enabled": True,
        "cup_type": "omni", "recommendation": True,
        "notes": "Rule-based starting recommendation from xBloom Local, not a roaster-provided recipe or a validated brew. "
                 + rationale + " Roast level, water chemistry and grinder calibration are not assumed. Taste and adjust grind first.",
        "pours": [{"volume_ml": volume, "temperature_c": temperature, "flow_ml_s": 3.0,
                   "pattern": pattern, "pause_s": pause, "agitation_before": False, "agitation_after": False}
                  for volume, temperature, pattern, pause in ((54, 92, "circle", 35), (78, 91, "spiral", 15),
                                                             (78, 90, "spiral", 15), (78, 90, "center", 0))],
    }


def profile_from_html(html, source_url):
    parser = _PageParser()
    try:
        parser.feed(html)
    except (ValueError, AssertionError) as exc:
        raise ProductLookupError("The website returned malformed HTML. Enter the bean details manually.") from exc
    products = [product for product in _products(parser.scripts) if _plain(product.get("name"), 200)]
    source_parts = urlsplit(source_url)
    def matching(product):
        value = product.get("url") or product.get("@id")
        if not isinstance(value, str):
            return False
        try:
            parts = urlsplit(urljoin(source_url, value))
            return (parts.scheme in {"http", "https"} and parts.port in {None, 443}
                    and parts.hostname == source_parts.hostname
                    and parts.path.rstrip("/") == source_parts.path.rstrip("/")
                    and (not _without_tracking(parts.query) or _without_tracking(parts.query) == _without_tracking(source_parts.query)))
        except ValueError:
            return False
    matches = [product for product in products if matching(product)]
    if len(matches) == 1:
        product = matches[0]
    elif len(products) == 1 and not products[0].get("url") and not products[0].get("@id"):
        product = products[0]
    else:
        raise ProductLookupError("A single matching product could not be identified. Paste a prepared profile or enter the bean details manually.")
    # Query parameters can identify distinct products; never silently discard them.
    canonical = validate_url(source_url)
    name = " ".join(_plain(product.get("name"), 500).split())
    if not name or len(name) > 160:
        raise ProductLookupError("The product name is missing or too long. Enter the bean details manually.")
    description = _plain(product.get("description"))
    facts = _labeled_facts(description)
    properties = product.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    if isinstance(properties, list):
        for prop in properties[:50]:
            if isinstance(prop, dict):
                label, value = _plain(prop.get("name"), 100), _plain(prop.get("value"), 500)
                field = _LABELS.get(label.casefold())
                if field and value:
                    facts.setdefault(field, value)
    bean = {"name": name, "product_url": canonical, "provenance": {
        "name": f"{canonical} — Product structured-data name.",
        "product_url": f"{canonical} — fetched product page.",
    }}
    brand = product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    roaster = " ".join(_plain(brand, 200).split()) or facts.get("roaster")
    if roaster and len(roaster) <= 160:
        bean["roaster"] = roaster
        bean["provenance"]["roaster"] = f"{canonical} — Product brand or explicit roaster label; review the attribution."
    for field in ("country", "region", "farm", "elevation", "process", "variety", "roast_level"):
        if facts.get(field):
            bean[field] = facts[field]
            bean["provenance"][field] = f"{canonical} — explicitly labeled {field.replace('_', ' ')}."
    if facts.get("roast_date"):
        try:
            bean["roast_date"] = date.fromisoformat(facts["roast_date"]).isoformat()
            bean["provenance"]["roast_date"] = f"{canonical} — explicitly labeled roast date."
        except ValueError:
            pass
    if facts.get("flavor_notes"):
        bean["flavor_notes"] = [note.strip() for note in re.split(r"[,;•]", facts["flavor_notes"]) if note.strip()][:30]
        bean["provenance"]["flavor_notes"] = f"{canonical} — labeled tasting notes, not inferred flavors."
    notes = []
    if facts.get("producer"):
        notes.append("Producer: " + facts["producer"] + ".")
    if facts.get("origin"):
        origin = facts["origin"]
        notes.append("Origin as listed: " + origin + ".")
        components = [part.strip() for part in origin.split(",")]
        if components[-1].casefold() in _COUNTRIES:
            bean.setdefault("country", components[-1])
            bean["provenance"].setdefault("country", f"{canonical} — country explicitly named in the Origin label.")
            # Split a three-part origin only when the named farm is also explicitly
            # identified as a finca/farm/estate in this product's own text.
            if len(components) == 3 and re.search(r"\b(?:finca|farm|estate)\s+" + re.escape(components[0]) + r"\b", name + "\n" + description, re.I):
                for field, value in (("farm", components[0]), ("region", components[1])):
                    bean.setdefault(field, value)
                    bean["provenance"].setdefault(field, f"{canonical} — Origin hierarchy, with farm identified in product text; review this split.")
    if notes:
        bean["notes"] = "\n".join(notes)
        bean["provenance"]["notes"] = f"{canonical} — explicit producer/origin labels."
    profile = Profile.model_validate({"schema_version": 1, "bean": bean, "recipe": recommended_recipe(bean)}).model_dump()
    unknowns = [field.replace("_", " ") for field in ("country", "region", "farm", "elevation", "process", "variety", "roast_level", "roast_date") if profile["bean"][field] is None]
    warnings = ["Review the extracted bean facts before saving. Website content cannot approve an import or a card write.",
                "The 18 g recipe is a rule-based starting recommendation, not a recipe supplied by the roaster."]
    if unknowns:
        warnings.append("Not found or not reliably identified; left unknown: " + ", ".join(unknowns) + ".")
    return {"profile": profile, "warnings": warnings, "source_url": canonical}


def lookup_product(url):
    html, source_url = fetch_page(url)
    return profile_from_html(html, source_url)
