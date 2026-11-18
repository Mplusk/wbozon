import json
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import marketplace_proxy as proxy


class RelayTests(unittest.TestCase):
    def test_allowlist_contains_only_required_marketplace_hosts(self):
        self.assertEqual(proxy.ALLOWED, {'api-seller.ozon.ru',
            'discounts-prices-api.wildberries.ru', 'content-api.wildberries.ru',
            'common-api.wildberries.ru', 'seller-analytics-api.wildberries.ru','supplies-api.wildberries.ru',
            'statistics-api.wildberries.ru','finance-api.wildberries.ru','feedbacks-api.wildberries.ru',
            'advert-api.wildberries.ru','documents-api.wildberries.ru','api-performance.ozon.ru',
            'dev.wildberries.ru','docs.ozon.com','api.avito.ru'})

    def run_connection(self, payload, resolver=None, connector=None):
        server, client = socket.socketpair()
        client.settimeout(2)
        calls = []
        def resolve(host):
            calls.append(host)
            return resolver(host) if resolver else ['8.8.8.8']
        def connect(addresses):
            return connector(addresses) if connector else None
        thread = threading.Thread(target=proxy.serve_connection, args=(server, resolve, connect), daemon=True)
        thread.start()
        client.sendall(payload)
        return client, thread, calls

    def test_rejects_any_other_method_destination_or_port_before_dns(self):
        for request in (
            b'GET http://api-seller.ozon.ru/ HTTP/1.1\r\n\r\n',
            b'CONNECT api-seller.ozon.ru:80 HTTP/1.1\r\n\r\n',
            b'CONNECT api-seller.ozon.ru.evil.example:443 HTTP/1.1\r\n\r\n',
            b'CONNECT evil.example:443 HTTP/1.1\r\n\r\n',
            b'CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n',
            b'CONNECT user@api-seller.ozon.ru:443 HTTP/1.1\r\n\r\n',
            b'CONNECT api-seller.ozon.ru:0443 HTTP/1.1\r\n\r\n',
        ):
            with self.subTest(request=request):
                client, thread, calls = self.run_connection(request)
                self.assertIn(b'403 Forbidden', client.recv(4096))
                thread.join(2); client.close()
                self.assertEqual(calls, [])
                self.assertFalse(thread.is_alive())

    def test_tunnels_only_allowed_hosts_and_relays_early_and_later_bytes(self):
        for host in sorted(proxy.ALLOWED):
            with self.subTest(host=host):
                upstream, peer = socket.socketpair()
                peer.settimeout(2)
                client, thread, calls = self.run_connection(
                    f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n'.encode() + b'early',
                    connector=lambda addresses: upstream)
                self.assertIn(b'200 Connection Established', client.recv(4096))
                self.assertEqual(peer.recv(5), b'early')
                peer.sendall(b'response')
                self.assertEqual(client.recv(8), b'response')
                client.sendall(b'later')
                self.assertEqual(peer.recv(5), b'later')
                client.close(); peer.close(); thread.join(2)
                self.assertEqual(calls, [host])
                self.assertFalse(thread.is_alive())

    def test_unresolved_host_fails_without_connecting(self):
        with self.subTest('dns fails'):
            client, thread, calls = self.run_connection(
                b'CONNECT api-seller.ozon.ru:443 HTTP/1.1\r\n\r\n', resolver=lambda host: [])
            self.assertIn(b'502 Bad Gateway', client.recv(4096))
            thread.join(2); client.close()
            self.assertEqual(calls, ['api-seller.ozon.ru'])

    def test_idle_timeout_closes_established_tunnel(self):
        upstream, peer = socket.socketpair()
        try:
            with patch.object(proxy, 'IDLE_TIMEOUT', 0.1):
                client, thread, _ = self.run_connection(
                    b'CONNECT api-seller.ozon.ru:443 HTTP/1.1\r\n\r\n', connector=lambda addresses: upstream)
                try:
                    self.assertIn(b'200 Connection Established', client.recv(4096))
                    self.assertEqual(client.recv(1), b'')
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                finally:
                    client.close()
        finally:
            peer.close()

    def test_tunnel_volume_limit_closes_before_forwarding_excess(self):
        upstream, peer = socket.socketpair()
        peer.settimeout(2)
        try:
            with patch.object(proxy, 'MAX_TUNNEL_BYTES', 4):
                client, thread, _ = self.run_connection(
                    b'CONNECT api-seller.ozon.ru:443 HTTP/1.1\r\n\r\n', connector=lambda addresses: upstream)
                try:
                    self.assertIn(b'200 Connection Established', client.recv(4096))
                    client.sendall(b'12345')
                    self.assertEqual(peer.recv(1), b'')
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                finally:
                    client.close()
        finally:
            peer.close()

    def test_total_timeout_applies_even_when_tunnel_is_active(self):
        upstream, peer = socket.socketpair()
        peer.settimeout(1)
        try:
            with patch.object(proxy, 'TOTAL_TIMEOUT', 0.2), patch.object(proxy, 'IDLE_TIMEOUT', 1):
                client, thread, _ = self.run_connection(
                    b'CONNECT api-seller.ozon.ru:443 HTTP/1.1\r\n\r\n', connector=lambda addresses: upstream)
                try:
                    self.assertIn(b'200 Connection Established', client.recv(4096))
                    for _ in range(10):
                        try:
                            client.sendall(b'x')
                            if not peer.recv(1): break
                        except OSError:
                            break
                        time.sleep(0.04)
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                finally:
                    client.close()
        finally:
            peer.close()

    def test_oversized_headers_and_slow_headers_are_bounded(self):
        client, thread, calls = self.run_connection(b'A' * proxy.MAX_HEADERS)
        self.assertIn(b'408 Request Timeout', client.recv(4096))
        client.close(); thread.join(2)
        self.assertEqual(calls, [])
        with patch.object(proxy, 'HEADER_TIMEOUT', 0.1):
            client, thread, calls = self.run_connection(b'C')
            self.assertIn(b'408 Request Timeout', client.recv(4096))
            client.close(); thread.join(2)
            self.assertEqual(calls, [])

    def test_dns_filters_private_loopback_linklocal_and_multicast(self):
        payload = json.dumps(['127.0.0.1', '10.0.0.1', '169.254.1.1', '::1', 'fc00::1',
                              '224.0.0.1', 'ff02::1', '8.8.8.8', '2606:4700:4700::1111']).encode()
        result = subprocess.CompletedProcess([], 0, stdout=payload)
        with patch('marketplace_proxy.subprocess.run', return_value=result):
            self.assertEqual(proxy.resolve_targets('api-seller.ozon.ru'), ['8.8.8.8', '2606:4700:4700::1111'])
        with patch('marketplace_proxy.subprocess.run', side_effect=subprocess.TimeoutExpired('dns', 3)):
            self.assertEqual(proxy.resolve_targets('api-seller.ozon.ru'), [])

    def test_server_binds_loopback_and_rejects_excess_clients(self):
        with patch.object(proxy, 'PORT', 0), patch.object(proxy, 'HEADER_TIMEOUT', 2):
            server = proxy.Server()
            self.assertEqual(server.server_address[0], '127.0.0.1')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            clients = []
            try:
                for _ in range(proxy.MAX_CLIENTS):
                    clients.append(socket.create_connection(server.server_address, timeout=2))
                    clients[-1].sendall(b'C')
                deadline = time.monotonic() + 2
                while server.slots._value and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.slots._value, 0)
                extra = socket.create_connection(server.server_address, timeout=2)
                try:
                    extra.settimeout(2)
                    self.assertIn(b'503 Service Unavailable', extra.recv(4096))
                finally:
                    extra.close()
            finally:
                for client in clients: client.close()
                server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
