#!/usr/bin/env python3
"""Loopback-only, bounded CONNECT relay for fixed marketplace API hosts."""
import ipaddress
import json
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time

HOST = '127.0.0.1'
PORT = 18761
ALLOWED = frozenset(('api-seller.ozon.ru', 'discounts-prices-api.wildberries.ru',
                     'content-api.wildberries.ru', 'common-api.wildberries.ru',
                     'seller-analytics-api.wildberries.ru','supplies-api.wildberries.ru',
                     'statistics-api.wildberries.ru','finance-api.wildberries.ru','feedbacks-api.wildberries.ru',
                     'advert-api.wildberries.ru','documents-api.wildberries.ru','api-performance.ozon.ru',
                     'dev.wildberries.ru','docs.ozon.com'))
MAX_CLIENTS = 8
MAX_HEADERS = 8192
HEADER_TIMEOUT = 5
DNS_TIMEOUT = 3
CONNECT_TIMEOUT = 5
IDLE_TIMEOUT = 20
TOTAL_TIMEOUT = 60
MAX_TUNNEL_BYTES = 8 * 1024 * 1024

RESOLVER = '''import json,socket,sys
items=socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)
print(json.dumps(list(dict.fromkeys(x[4][0] for x in items))[:16]))
'''


def resolve_targets(host):
    # A separate bounded process prevents stuck DNS from consuming all threads.
    try:
        result = subprocess.run([sys.executable, '-c', RESOLVER, host], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=DNS_TIMEOUT, check=True)
        values = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if not isinstance(values, list):
        return []
    out = []
    for value in values[:16]:
        try:
            addr = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            continue
        if addr.is_global and not addr.is_multicast:
            out.append(str(addr))
    return out


def connect_target(addresses):
    deadline = time.monotonic() + CONNECT_TIMEOUT
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ip = ipaddress.ip_address(address)
        sock = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(remaining)
            sock.connect((address, 443))
            return sock
        except OSError:
            sock.close()
    return None


def response(client, status):
    try:
        client.sendall(('HTTP/1.1 ' + status + '\r\nConnection: close\r\nContent-Length: 0\r\n\r\n').encode('ascii'))
    except OSError:
        pass


def serve_connection(client, resolver=resolve_targets, connector=connect_target):
    upstream = None
    started = time.monotonic()
    try:
        # Absolute deadline, not a timeout reset by each received byte.
        header_end = started + HEADER_TIMEOUT
        header = bytearray()
        while b'\r\n\r\n' not in header:
            remaining = header_end - time.monotonic()
            if remaining <= 0 or len(header) >= MAX_HEADERS:
                response(client, '408 Request Timeout')
                return
            client.settimeout(remaining)
            try:
                part = client.recv(MAX_HEADERS - len(header))
            except socket.timeout:
                client.settimeout(1)
                response(client, '408 Request Timeout')
                return
            if not part:
                return
            header.extend(part)
        end = header.index(b'\r\n\r\n') + 4
        head, early_data = bytes(header[:end]), bytes(header[end:])
        if any(byte > 127 or (byte < 32 and byte not in (9, 10, 13)) for byte in head):
            response(client, '400 Bad Request')
            return
        lines = head[:-4].split(b'\r\n')
        fields = lines[0].split(b' ')
        if len(fields) != 3 or fields[0] != b'CONNECT' or fields[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
            response(client, '403 Forbidden')
            return
        try:
            authority = fields[1].decode('ascii')
            host, port = authority.rsplit(':', 1)
        except (UnicodeError, ValueError):
            response(client, '403 Forbidden')
            return
        if host not in ALLOWED or port != '443':
            response(client, '403 Forbidden')
            return
        addresses = resolver(host)
        upstream = connector(addresses) if addresses else None
        if upstream is None:
            response(client, '502 Bad Gateway')
            return
        client.settimeout(5)
        upstream.settimeout(5)
        client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        if early_data:
            upstream.sendall(early_data)
        transferred = len(early_data)
        deadline = started + TOTAL_TIMEOUT
        idle_deadline = time.monotonic() + IDLE_TIMEOUT
        while True:
            remaining = min(deadline, idle_deadline) - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([client, upstream], [], [], remaining)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                transferred += len(data)
                if transferred > MAX_TUNNEL_BYTES:
                    return
                (upstream if source is client else client).sendall(data)
                idle_deadline = time.monotonic() + IDLE_TIMEOUT
    except (OSError, ValueError):
        # Never log exception text, headers, destinations, or tunnel data.
        return
    finally:
        if upstream is not None:
            upstream.close()
        client.close()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        serve_connection(self.request)


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CLIENTS
    def __init__(self):
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__((HOST, PORT), Handler)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.settimeout(0.2)
            response(request, '503 Service Unavailable')
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
    def handle_error(self, request, client_address):
        pass


if __name__ == '__main__':
    with Server() as server:
        server.serve_forever(poll_interval=0.5)
