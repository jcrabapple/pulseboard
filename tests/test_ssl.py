"""Tests for SSL certificate expiry checker."""

from __future__ import annotations

import asyncio
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from pulseboard.models import ServiceConfig, ServiceType, Status
from pulseboard.ssl_check import check_ssl, parse_host_port, probe_cert


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _generate_self_signed_cert(
    days_valid: int = 365, cn: str = "localhost", in_the_past: bool = False
) -> tuple[bytes, bytes]:
    """Generate a self-signed cert + key pair using only stdlib.

    Returns ``(cert_pem, key_pem)``. Used by the local TLS test server.
    When ``in_the_past=True``, both ``not_valid_before`` and ``not_valid_after``
    are placed in the past (needed for testing "already expired" scenarios).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    )
    now = datetime.now(timezone.utc)
    if in_the_past:
        not_before = now - timedelta(days=abs(days_valid) + 10)
        not_after = now + timedelta(days=days_valid)
    else:
        not_before = now - timedelta(minutes=5)
        not_after = now + timedelta(days=days_valid)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(cn)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


class _LocalTlsServer:
    """Tiny TLS server that accepts a single connection and serves one cert.

    Used as a deterministic target for SSL probe tests.
    """

    def __init__(self, cert_pem: bytes, key_pem: bytes):
        self.cert_pem = cert_pem
        self.key_pem = key_pem
        self._server: ssl.SSLContext | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.host = "127.0.0.1"
        self.port = 0  # OS-assigned

    def start(self) -> None:
        # Write cert + key to tempfiles the SSLContext can load.
        import tempfile

        cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        cert_file.write(self.cert_pem)
        cert_file.close()
        key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        key_file.write(self.key_pem)
        key_file.close()

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file.name, key_file.name)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        sock.listen(8)
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._ctx = ctx
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        # Give the thread a moment to enter accept().
        time.sleep(0.05)

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _serve(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(2.0)
        try:
            while not self._stop.is_set():
                try:
                    client, _ = self._sock.accept()
                except (socket.timeout, OSError):
                    return
                try:
                    tls = self._ctx.wrap_socket(client, server_side=True)
                    tls.recv(1)
                    tls.close()
                except (ssl.SSLError, OSError):
                    pass
        except Exception:
            return


@pytest.fixture
def long_lived_cert_server():
    """A TLS server presenting a 365-day cert (healthy)."""
    cert_pem, key_pem = _generate_self_signed_cert(days_valid=365, cn="localhost")
    server = _LocalTlsServer(cert_pem, key_pem)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def expiring_cert_server():
    """A TLS server presenting a cert expiring in 7 days (degraded)."""
    cert_pem, key_pem = _generate_self_signed_cert(days_valid=7, cn="localhost")
    server = _LocalTlsServer(cert_pem, key_pem)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def expired_cert_server():
    """A TLS server presenting an already-expired cert (down)."""
    cert_pem, key_pem = _generate_self_signed_cert(
        days_valid=-3, cn="localhost", in_the_past=True
    )
    server = _LocalTlsServer(cert_pem, key_pem)
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# parse_host_port tests (no network)
# ---------------------------------------------------------------------------


def test_parse_host_port_explicit_fields():
    svc = ServiceConfig(name="x", url="", host="example.com", port=8443,
                        service_type=ServiceType.SSL)
    assert parse_host_port(svc) == ("example.com", 8443)


def test_parse_host_port_https_url_default_port():
    svc = ServiceConfig(name="x", url="https://example.com/foo",
                        service_type=ServiceType.SSL)
    assert parse_host_port(svc) == ("example.com", 443)


def test_parse_host_port_https_url_with_port():
    svc = ServiceConfig(name="x", url="https://example.com:8443/foo",
                        service_type=ServiceType.SSL)
    assert parse_host_port(svc) == ("example.com", 8443)


def test_parse_host_port_empty_falls_back_to_443():
    svc = ServiceConfig(name="x", url="", service_type=ServiceType.SSL)
    host, port = parse_host_port(svc)
    assert host == ""
    assert port == 443


# ---------------------------------------------------------------------------
# check_ssl integration tests (real TLS handshake)
# ---------------------------------------------------------------------------


def test_check_ssl_healthy_cert(long_lived_cert_server):
    svc = ServiceConfig(
        name="healthy",
        url="",
        service_type=ServiceType.SSL,
        host=long_lived_cert_server.host,
        port=long_lived_cert_server.port,
        timeout=5,
    )
    result = asyncio.run(check_ssl(svc))
    assert result.status == Status.UP
    assert result.error is None
    days = result.details["days_until_expiry"]
    assert days is not None and days > 30
    assert result.details["issuer"] != "<unknown>"
    assert result.latency_ms >= 0


def test_check_ssl_expiring_cert(expiring_cert_server):
    svc = ServiceConfig(
        name="expiring",
        url="",
        service_type=ServiceType.SSL,
        host=expiring_cert_server.host,
        port=expiring_cert_server.port,
        timeout=5,
        ssl_expiry_warning_days=14,
    )
    result = asyncio.run(check_ssl(svc))
    assert result.status == Status.DEGRADED
    assert result.error is not None
    assert "expires in" in result.error.lower()
    days = result.details["days_until_expiry"]
    assert 0 < days <= 7


def test_check_ssl_expired_cert(expired_cert_server):
    svc = ServiceConfig(
        name="expired",
        url="",
        service_type=ServiceType.SSL,
        host=expired_cert_server.host,
        port=expired_cert_server.port,
        timeout=5,
    )
    result = asyncio.run(check_ssl(svc))
    assert result.status == Status.DOWN
    assert result.error is not None
    assert "expired" in result.error.lower()


def test_check_ssl_connection_failure():
    svc = ServiceConfig(
        name="unreachable",
        url="",
        service_type=ServiceType.SSL,
        host="127.0.0.1",
        port=1,  # unassigned / closed port
        timeout=2,
    )
    result = asyncio.run(check_ssl(svc))
    assert result.status == Status.DOWN
    assert result.error is not None


def test_check_ssl_warning_threshold_makes_long_cert_healthy():
    """A 365-day cert with a 14-day warning window should be UP."""
    cert_pem, key_pem = _generate_self_signed_cert(days_valid=365, cn="localhost")
    server = _LocalTlsServer(cert_pem, key_pem)
    server.start()
    try:
        svc = ServiceConfig(
            name="t",
            url="",
            service_type=ServiceType.SSL,
            host=server.host,
            port=server.port,
            timeout=5,
            ssl_expiry_warning_days=14,
        )
        result = asyncio.run(check_ssl(svc))
        assert result.status == Status.UP
    finally:
        server.stop()


def test_check_ssl_short_warning_marks_long_cert_degraded():
    """A 365-day cert with a 1000-day warning window should be DEGRADED."""
    cert_pem, key_pem = _generate_self_signed_cert(days_valid=365, cn="localhost")
    server = _LocalTlsServer(cert_pem, key_pem)
    server.start()
    try:
        svc = ServiceConfig(
            name="t",
            url="",
            service_type=ServiceType.SSL,
            host=server.host,
            port=server.port,
            timeout=5,
            ssl_expiry_warning_days=1000,
        )
        result = asyncio.run(check_ssl(svc))
        assert result.status == Status.DEGRADED
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# probe_cert unit tests
# ---------------------------------------------------------------------------


def test_probe_cert_returns_details_dict(long_lived_cert_server):
    svc = ServiceConfig(
        name="t",
        url="",
        service_type=ServiceType.SSL,
        host=long_lived_cert_server.host,
        port=long_lived_cert_server.port,
    )
    details, elapsed = probe_cert(
        svc, long_lived_cert_server.host, long_lived_cert_server.port, 5.0
    )
    assert "not_after" in details
    assert "issuer" in details
    assert "subject" in details
    assert "days_until_expiry" in details
    assert elapsed >= 0