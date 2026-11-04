from email.message import Message
import ipaddress
import json
import socket
import ssl
import time

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app import product_import as importer


SOURCE = "https://roaster.example/products/test-coffee"
PUBLIC_IP = "93.184.216.34"


def product_html(**changes):
    product = {"@type": "Product", "name": "Synthetic Gesha - Finca Example", "url": SOURCE,
               "brand": {"name": "Example Coffee"}, "description": (
                   "Tastes Like: Apricot, Black Tea\nOrigin: Example, Huila, Colombia\nVariety: Gesha\n"
                   "Producer: Example grower\nElevation: 1800 masl\nProcess: Extended fermentation")}
    product.update(changes)
    return '<script type="application/ld+json">' + json.dumps(product) + "</script>"


def dns_answers(*addresses):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses]


@pytest.mark.parametrize("url", [
    "http://roaster.example/product", "file:///etc/passwd", "https://user:password@roaster.example/product",
    "https://roaster.example:8443/product", "https://localhost/product", "https://127.0.0.1/",
    "https://192.168.1.39/", "https://169.254.169.254/", "https://[::1]/", "https://[fc00::1]/",
    "https://[::ffff:127.0.0.1]/", "https://[64:ff9b::7f00:1]/", "https://2130706433/",
    "https://roaster.example/\r\nHost: localhost", "https://roaster.example\\@127.0.0.1/",
    "https://[bad/", "https://roaster.example/a b", "https://roaster.example/" + "x" * 2048,
])
def test_url_lookup_rejects_unsafe_destinations(url):
    with pytest.raises(importer.ProductLookupError):
        importer.validate_url(url)


def test_url_normalization_keeps_product_identity_query():
    assert importer.validate_url("https://ROASTER.example:443/product?id=A#details") == "https://roaster.example/product?id=A"


def test_shopify_sign_in_parameters_are_removed_before_fetch_and_provenance(monkeypatch):
    url = SOURCE + "?shop_consented_scopes=email%3Averified+openid+pay%3Asession_token+profile+shop%3Aaccount_uuid&shop_sign_in=true"
    fetched = []
    monkeypatch.setattr(importer, "_resolve_public", lambda *args: [ipaddress.ip_address(PUBLIC_IP)])
    def fetch(clean_url, *args):
        fetched.append(clean_url)
        return product_html(), None
    monkeypatch.setattr(importer, "_fetch_hop", fetch)
    result = importer.lookup_product(url)
    assert fetched == [SOURCE]
    assert result["source_url"] == SOURCE and result["profile"]["bean"]["product_url"] == SOURCE
    assert all("shop_" not in value for value in result["profile"]["bean"]["provenance"].values())
    assert importer.validate_url(SOURCE + "?id=A&shop_sign_in=true&variant=123&utm_source=email") == SOURCE + "?id=A&variant=123"


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["192.168.1.39"], ["169.254.169.254"], ["::1"], ["fc00::1"],
    [PUBLIC_IP, "10.0.0.1"], [PUBLIC_IP, "::ffff:127.0.0.1"], ["192.0.2.1"],
])
def test_dns_requires_every_answer_to_be_public(monkeypatch, addresses):
    monkeypatch.setattr(importer.socket, "getaddrinfo", lambda *args, **kwargs: dns_answers(*addresses))
    with pytest.raises(importer.ProductLookupError):
        importer._resolve_public("roaster.example", time.monotonic() + 5)


class FakeSocket:
    def __init__(self):
        self.connected = None
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, target):
        self.connected = target

    def close(self):
        self.closed = True

    def shutdown(self, how):
        self.closed = True


def test_connection_pins_resolved_address_and_preserves_tls_hostname(monkeypatch):
    dns_calls = []
    def changing_dns(*args, **kwargs):
        dns_calls.append(args[0])
        return dns_answers(PUBLIC_IP if len(dns_calls) == 1 else "127.0.0.1")
    monkeypatch.setattr(importer.socket, "getaddrinfo", changing_dns)
    deadline = time.monotonic() + 5
    address = importer._resolve_public("roaster.example", deadline)[0]
    connection = importer._PinnedHTTPSConnection("roaster.example", address, deadline)
    assert connection._context.check_hostname is True
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    sock = FakeSocket()
    monkeypatch.setattr(importer.socket, "socket", lambda *args: sock)
    tls_names = []
    class FakeContext:
        def wrap_socket(self, raw, server_hostname):
            tls_names.append(server_hostname)
            return raw
    connection._context = FakeContext()
    connection.connect()
    assert sock.connected == (PUBLIC_IP, 443)
    assert dns_calls == ["roaster.example"]
    assert tls_names == ["roaster.example"]
    connection.close()


def test_redirect_revalidates_destination_dns_before_connecting(monkeypatch):
    looked_up, fetched = [], []
    def resolver(host, *args, **kwargs):
        looked_up.append(host)
        return dns_answers(PUBLIC_IP if host == "roaster.example" else "192.168.1.39")
    def fetch(url, address, deadline):
        fetched.append((url, str(address)))
        return None, "https://internal.example/private"
    monkeypatch.setattr(importer.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(importer, "_fetch_hop", fetch)
    with pytest.raises(importer.ProductLookupError):
        importer.fetch_page(SOURCE)
    assert looked_up == ["roaster.example", "internal.example"]
    assert fetched == [(SOURCE, PUBLIC_IP)]


@pytest.mark.parametrize("destination", ["http://roaster.example/insecure", "https://user:secret@roaster.example/private", "https://127.0.0.1/private"])
def test_redirect_cannot_downgrade_or_target_local_addresses(monkeypatch, destination):
    monkeypatch.setattr(importer, "_resolve_public", lambda *args: [ipaddress.ip_address(PUBLIC_IP)])
    monkeypatch.setattr(importer, "_fetch_hop", lambda *args: (None, destination))
    with pytest.raises(importer.ProductLookupError):
        importer.fetch_page(SOURCE)


def test_redirect_count_is_bounded(monkeypatch):
    hops = []
    monkeypatch.setattr(importer, "_resolve_public", lambda *args: [ipaddress.ip_address(PUBLIC_IP)])
    def redirect(url, *args):
        hops.append(url)
        return None, f"https://roaster.example/hop-{len(hops)}"
    monkeypatch.setattr(importer, "_fetch_hop", redirect)
    with pytest.raises(importer.ProductLookupError, match="too many redirects"):
        importer.fetch_page(SOURCE)
    assert len(hops) == importer.MAX_REDIRECTS + 1


class FakeResponse:
    def __init__(self, body=b"<html>example</html>", status=200, **headers):
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        for key, value in headers.items():
            if key in self.headers:
                self.headers.replace_header(key, value)
            else:
                self.headers[key] = value
        self.body = body
        self.position = 0
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, length):
        self.reads += 1
        result = self.body[self.position:self.position + length]
        self.position += len(result)
        return result


def fake_connection(monkeypatch, response):
    connections = []
    class Connection:
        def __init__(self, host, address, deadline):
            self.host, self.address = host, address
            self.transport_socket = FakeSocket()
            self.closed = False
            connections.append(self)

        def request(self, method, target, headers):
            self.method, self.target, self.headers = method, target, headers

        def getresponse(self):
            return response

        def abort(self):
            self.transport_socket.close()

        def close(self):
            self.closed = True
    monkeypatch.setattr(importer, "_PinnedHTTPSConnection", Connection)
    return connections


def test_fetch_has_only_explicit_noncredential_headers(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    response = FakeResponse()
    connections = fake_connection(monkeypatch, response)
    html, redirect = importer._fetch_hop(SOURCE + "?id=A", ipaddress.ip_address(PUBLIC_IP), time.monotonic() + 5)
    assert html == "<html>example</html>" and redirect is None
    connection = connections[0]
    assert str(connection.address) == PUBLIC_IP and connection.host == "roaster.example"
    assert connection.method == "GET" and connection.target == "/products/test-coffee?id=A"
    assert connection.headers["Host"] == "roaster.example"
    assert set(connection.headers) == {"Host", "User-Agent", "Accept", "Accept-Encoding"}
    assert connection.closed


@pytest.mark.parametrize("headers,body", [
    ({"Content-Length": "200"}, b"short"), ({}, b"x" * 129),
    ({"Content-Encoding": "gzip"}, b"compressed"), ({"Content-Type": "application/octet-stream"}, b"binary"),
])
def test_fetch_bounds_body_and_rejects_compression_and_non_html(monkeypatch, headers, body):
    monkeypatch.setattr(importer, "MAX_BYTES", 128)
    connections = fake_connection(monkeypatch, FakeResponse(body=body, **headers))
    with pytest.raises(importer.ProductLookupError):
        importer._fetch_hop(SOURCE, ipaddress.ip_address(PUBLIC_IP), time.monotonic() + 5)
    assert connections[0].closed


def test_fetch_stops_when_absolute_deadline_passes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(importer.time, "monotonic", lambda: clock[0])
    response = FakeResponse()
    def slow_read(length):
        clock[0] += 6
        return b"x"
    response.read1 = slow_read
    connections = fake_connection(monkeypatch, response)
    with pytest.raises(importer.ProductLookupError) as caught:
        importer._fetch_hop(SOURCE, ipaddress.ip_address(PUBLIC_IP), 105.0)
    assert caught.value.status_code == 504
    assert connections[0].closed


def test_extracts_labeled_facts_separately_from_recommendation():
    result = importer.profile_from_html(product_html(), SOURCE)
    bean, recipe = result["profile"]["bean"], result["profile"]["recipe"]
    assert bean["name"] == "Synthetic Gesha - Finca Example"
    assert bean["roaster"] == "Example Coffee"
    assert (bean["farm"], bean["region"], bean["country"]) == ("Example", "Huila", "Colombia")
    assert bean["elevation"] == "1800 masl" and bean["process"] == "Extended fermentation"
    assert bean["variety"] == "Gesha" and bean["flavor_notes"] == ["Apricot", "Black Tea"]
    assert "Example grower" in bean["notes"]
    assert bean["roast_level"] is None and bean["roast_date"] is None
    assert all(SOURCE in value for value in bean["provenance"].values())
    assert recipe["recommendation"] and "not a roaster-provided recipe" in recipe["notes"]
    assert recipe["dose_g"] == 18 and recipe["ratio"] == 16 and recipe["total_water_ml"] == 288
    assert [pour["volume_ml"] for pour in recipe["pours"]] == [54, 78, 78, 78]
    assert [pour["temperature_c"] for pour in recipe["pours"]] == [92, 91, 90, 90]
    assert all(not pour["agitation_before"] and not pour["agitation_after"] for pour in recipe["pours"])


def test_uses_only_this_products_facts_not_scripts_or_page_wide_labels():
    html = product_html(description="This coffee has a delicate character.", brand=None)
    html += '<p>Country: Imaginary</p><script>raise Error("execute me")</script>'
    result = importer.profile_from_html(html, SOURCE)["profile"]["bean"]
    assert result["country"] is None and result["roaster"] is None and result["flavor_notes"] == []
    assert result["roast_date"] is None and result["roast_level"] is None


def test_ambiguous_inline_labels_are_not_combined_into_one_fact():
    bean = importer.profile_from_html(product_html(description="Tastes Like: Apricot Origin: Colombia Process: Washed"), SOURCE)["profile"]["bean"]
    assert bean["flavor_notes"] == [] and bean["country"] is None and bean["process"] is None


def test_preserves_unknown_origin_fields_without_guessing_hierarchy():
    bean = importer.profile_from_html(product_html(name="Synthetic coffee", description="Origin: Mystery, Huila, Colombia"), SOURCE)["profile"]["bean"]
    assert bean["country"] == "Colombia" and bean["farm"] is None and bean["region"] is None
    assert "Mystery, Huila, Colombia" in bean["notes"]


def test_reads_additional_properties_and_only_explicit_iso_roast_dates():
    result = importer.profile_from_html(product_html(description="", additionalProperty=[
        {"name": "Process", "value": "Washed"}, {"name": "Roast level", "value": "Light"},
        {"name": "Roast date", "value": "2026-10-24"}, {"name": "Country", "value": "Peru"},
    ]), SOURCE)
    bean = result["profile"]["bean"]
    assert bean["roast_level"] == "Light" and bean["roast_date"] == "2026-10-24" and bean["country"] == "Peru"
    bean = importer.profile_from_html(product_html(description="Roast date: Freshly roasted weekly"), SOURCE)["profile"]["bean"]
    assert bean["roast_date"] is None


def test_product_matching_preserves_query_and_rejects_conflicting_identity():
    source = SOURCE + "?id=A"
    result = importer.profile_from_html(product_html(url=source), source)
    assert result["source_url"] == source and result["profile"]["bean"]["product_url"] == source
    assert all(source in note for note in result["profile"]["bean"]["provenance"].values())
    with pytest.raises(importer.ProductLookupError):
        importer.profile_from_html(product_html(url=SOURCE + "?id=B"), source)


def test_malformed_unrelated_product_metadata_does_not_crash_matching():
    html = product_html() + product_html(name="Unrelated", url="https://[bad")
    assert importer.profile_from_html(html, SOURCE)["profile"]["bean"]["name"] == "Synthetic Gesha - Finca Example"


@pytest.mark.parametrize("html", [
    "<title>A coffee article</title><p>Country: Colombia</p>",
    '<script type="application/ld+json">{"@type":"Article","name":"An article"}</script>',
    '<script type="application/ld+json">{invalid JSON}</script>',
    product_html(url="https://roaster.example/products/different"),
    "<![foo]>",
])
def test_does_not_invent_product_identity_from_generic_pages(html):
    with pytest.raises(importer.ProductLookupError):
        importer.profile_from_html(html, SOURCE)


def test_from_url_endpoint_requires_access_and_never_persists_before_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("XBLOOM_AUTH_MODE", "trusted_lan")
    monkeypatch.setenv("XBLOOM_ALLOWED_HOSTS", "testserver")
    fetched = []
    def fetch(url):
        fetched.append(url)
        return product_html(), SOURCE
    monkeypatch.setattr(importer, "fetch_page", fetch)
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        assert client.post("/api/import/from-url", json={"url": SOURCE}).status_code == 401
        assert fetched == []
        client.get("/api/session")
        response = client.post("/api/import/from-url", json={"url": SOURCE})
        assert response.status_code == 200 and fetched == [SOURCE]
        profile = response.json()["profile"]
        assert client.get("/api/beans").json()["items"] == []
        assert client.get("/api/recipes").json()["items"] == []
        assert client.post("/api/import/preview", json={"profile": profile}).status_code == 200
        assert client.post("/api/import/approve", json={"profile": profile}).status_code == 422
        assert client.get("/api/beans").json()["items"] == []
        approved = client.post("/api/import/approve", json={"profile": profile, "approved": True})
        assert approved.status_code == 200
        assert len(client.get("/api/beans").json()["items"]) == 1
        assert len(client.get("/api/recipes").json()["items"]) == 1
        assert client.get("/api/jobs").json()["items"] == []
