"""Bounded HTTP transport with one wall-clock deadline for every phase."""

from __future__ import annotations

import select
import socket
import ssl
from dataclasses import dataclass
from http.client import HTTPException, HTTPResponse
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
from urllib.parse import SplitResult, urlsplit


class HTTPDeadlineExceeded(TimeoutError):
    """The complete HTTP exchange exceeded its wall-clock budget."""


class HTTPTransportError(RuntimeError):
    """The HTTP exchange failed without exposing transport internals."""


class HTTPResponseTooLarge(ValueError):
    """The HTTP response exceeded its configured byte ceiling."""


@dataclass(frozen=True, slots=True)
class HTTPResult:
    status: int
    body: bytes


def post_bytes(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout_seconds: float,
    max_response_bytes: int,
) -> HTTPResult:
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be positive")
    if max_response_bytes < 1:
        raise ValueError("HTTP response limit must be positive")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HTTP URL is invalid")
    deadline = monotonic() + timeout_seconds
    connection = None
    response = None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection = _connect(parsed.hostname, port, deadline)
        if parsed.scheme == "https":
            connection = _negotiate_tls(
                connection,
                parsed.hostname,
                deadline,
            )
        request = _build_request(parsed, port, body, headers)
        connection.settimeout(_remaining(deadline))
        connection.sendall(request)
        response = _begin_response(connection, deadline)
        collected = bytearray()
        while True:
            connection.settimeout(_remaining(deadline))
            chunk = response.read1(
                min(65_536, max_response_bytes + 1 - len(collected))
            )
            if not chunk:
                break
            collected.extend(chunk)
            if len(collected) > max_response_bytes:
                raise HTTPResponseTooLarge(
                    f"HTTP response exceeds {max_response_bytes} bytes"
                )
        return HTTPResult(response.status, bytes(collected))
    except HTTPDeadlineExceeded:
        raise
    except TimeoutError as error:
        raise HTTPDeadlineExceeded("HTTP wall-clock deadline exceeded") from error
    except HTTPResponseTooLarge:
        raise
    except (OSError, HTTPException, ssl.SSLError) as error:
        raise HTTPTransportError("HTTP transport failed") from error
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    addresses = _resolve(host, port, deadline)
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(_remaining(deadline))
            connection.connect(address)
            return connection
        except TimeoutError as error:
            connection.close()
            raise HTTPDeadlineExceeded(
                "HTTP wall-clock deadline exceeded"
            ) from error
        except OSError:
            connection.close()
            if monotonic() >= deadline:
                raise HTTPDeadlineExceeded(
                    "HTTP wall-clock deadline exceeded"
                ) from None
    raise HTTPTransportError("HTTP transport failed")


def _resolve(host: str, port: int, deadline: float):
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def resolve() -> None:
        try:
            value = socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except BaseException as error:
            result.put((False, error))
        else:
            result.put((True, value))

    Thread(target=resolve, name="http-dns", daemon=True).start()
    try:
        succeeded, value = result.get(timeout=_remaining(deadline))
    except Empty:
        raise HTTPDeadlineExceeded("HTTP wall-clock deadline exceeded") from None
    if not succeeded:
        raise HTTPTransportError("HTTP transport failed") from value
    return value


def _negotiate_tls(
    connection: socket.socket,
    host: str,
    deadline: float,
) -> ssl.SSLSocket:
    wrapped = ssl.create_default_context().wrap_socket(
        connection,
        server_hostname=host,
        do_handshake_on_connect=False,
    )
    connection.close()
    wrapped.setblocking(False)
    try:
        while True:
            try:
                wrapped.do_handshake()
                break
            except ssl.SSLWantReadError:
                _wait_for_socket(wrapped, read=True, deadline=deadline)
            except ssl.SSLWantWriteError:
                _wait_for_socket(wrapped, read=False, deadline=deadline)
    except BaseException:
        wrapped.close()
        raise
    wrapped.setblocking(True)
    wrapped.settimeout(_remaining(deadline))
    return wrapped


def _wait_for_socket(
    connection: socket.socket,
    *,
    read: bool,
    deadline: float,
) -> None:
    readers = [connection] if read else []
    writers = [] if read else [connection]
    ready_read, ready_write, _ = select.select(
        readers,
        writers,
        [],
        _remaining(deadline),
    )
    if not ready_read and not ready_write:
        raise HTTPDeadlineExceeded("HTTP wall-clock deadline exceeded")


def _begin_response(
    connection: socket.socket,
    deadline: float,
) -> HTTPResponse:
    response = HTTPResponse(connection)
    result: Queue[BaseException | None] = Queue(maxsize=1)
    abandoned = Event()

    def begin() -> None:
        try:
            response.begin()
        except BaseException as error:
            result.put(error)
        else:
            result.put(None)
        finally:
            if abandoned.is_set():
                response.close()

    Thread(target=begin, name="http-response", daemon=True).start()
    try:
        error = result.get(timeout=_remaining(deadline))
    except Empty:
        abandoned.set()
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        response.close()
        raise HTTPDeadlineExceeded("HTTP wall-clock deadline exceeded") from None
    if error is not None:
        response.close()
        if isinstance(error, TimeoutError):
            raise HTTPDeadlineExceeded(
                "HTTP wall-clock deadline exceeded"
            ) from error
        raise HTTPTransportError("HTTP transport failed") from error
    return response


def _build_request(
    parsed: SplitResult,
    port: int,
    body: bytes,
    headers: dict[str, str],
) -> bytes:
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        path.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("HTTP URL path must be percent-encoded") from error
    host = parsed.hostname or ""
    host_header = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port != default_port:
        host_header = f"{host_header}:{port}"
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {host_header}",
        "Accept-Encoding: identity",
        "Connection: close",
        f"Content-Length: {len(body)}",
    ]
    reserved = {"host", "content-length", "connection"}
    for name, value in headers.items():
        if (
            not name
            or name.casefold() in reserved
            or any(character in name for character in "\r\n:")
            or any(character in value for character in "\r\n")
        ):
            raise ValueError("HTTP header is invalid")
        lines.append(f"{name}: {value}")
    try:
        head = "\r\n".join(lines).encode("latin-1")
    except UnicodeEncodeError as error:
        raise ValueError("HTTP headers must use Latin-1 characters") from error
    return head + b"\r\n\r\n" + body


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise HTTPDeadlineExceeded("HTTP wall-clock deadline exceeded")
    return remaining
