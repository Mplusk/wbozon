"""Bounded protocol and peer tests; no live database or network is used."""
import base64
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

if os.name == "posix":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-release"))
    import restore_broker as broker


@unittest.skipUnless(os.name == "posix", "Linux socket broker")
class BrokerTests(unittest.TestCase):
    def framed(self, text):
        raw = text.encode()
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        left.sendall(struct.pack("!I", len(raw)) + raw)
        return right

    def test_bounded_form_decodes_identity_without_echoing_it(self):
        value = {"action": "restore", "request_id": "A" * 22,
                 "archive": "wbozon-20261127T013053Z-0123456789ab.tar.gz.age",
                 "identity": base64.b64encode(b"synthetic-private-key" * 3).decode()}
        request = broker.receive(self.framed(json.dumps(value)))
        self.assertEqual(request["identity"], b"synthetic-private-key" * 3)

    def test_duplicate_keys_and_invalid_paths_are_rejected(self):
        cases = ['{"action":"list","action":"list"}',
                 json.dumps({"action": "restore", "request_id": "A" * 22,
                             "archive": "../outside", "identity": base64.b64encode(b"x" * 60).decode()})]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(broker.BrokerError):
                broker.receive(self.framed(value))

    def test_peer_must_belong_to_active_portal_cgroup(self):
        stream = MagicMock()
        stream.getsockopt.return_value = struct.pack("3i", 123, 400, 400)
        host = MagicMock()
        host.props.return_value = {"ActiveState": "active", "MainPID": "456", "ControlGroup": "/portal"}
        with patch.object(broker, "process_uid", return_value=400), \
                patch.object(broker, "process_identity", side_effect=[("a", "/portal"), ("b", "/portal")]):
            broker.check_peer(stream, host)
        with patch.object(broker, "process_uid", return_value=401):
            with self.assertRaisesRegex(broker.BrokerError, "invalid_peer"):
                broker.check_peer(stream, host)
        with patch.object(broker, "process_uid", return_value=400), \
                patch.object(broker, "process_identity", side_effect=[("a", "/portal"), ("b", "/other")]):
            with self.assertRaisesRegex(broker.BrokerError, "invalid_peer"):
                broker.check_peer(stream, host)

    def test_duplicate_request_is_not_executed_again(self):
        request = {"action": "restore", "request_id": "A" * 22,
                   "archive": "wbozon-20261127T013053Z-0123456789ab.tar.gz.age", "identity": b"x" * 50}
        host, transaction, stream = MagicMock(), MagicMock(), MagicMock()
        host.locked.return_value = nullcontext()
        transaction.pending.exists.return_value = False
        with patch.object(broker, "Host", return_value=host), patch.object(broker, "check_peer"), \
                patch.object(broker, "receive", return_value=request), patch.object(broker, "config_for", return_value={}), \
                patch.object(broker, "settings_lock", return_value=nullcontext()), \
                patch.object(broker.cd, "Transaction", return_value=transaction), \
                patch.object(broker, "_claim", return_value=False), patch.object(broker, "reply") as reply, \
                patch.object(broker, "run_restore") as run:
            broker.serve(stream)
            reply.assert_called_once_with(stream, {"status": "accepted"})
            run.assert_not_called()

    def test_resume_removes_only_a_confirmed_restore_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'hold'
            host, transaction, stream = MagicMock(), MagicMock(), MagicMock()
            host.locked.return_value = nullcontext()
            transaction.pending.exists.return_value = False
            transaction.market_hold = marker
            common = (patch.object(broker, 'Host', return_value=host), patch.object(broker, 'check_peer'),
                      patch.object(broker, 'receive', return_value={'action': 'resume'}),
                      patch.object(broker, 'config_for', return_value={}),
                      patch.object(broker.cd, 'Transaction', return_value=transaction),
                      patch.object(broker, 'secure', side_effect=lambda path, *args: Path(path)),
                      patch.object(broker, 'reply'))
            from contextlib import ExitStack
            with ExitStack() as stack:
                for item in common: stack.enter_context(item)
                marker.write_text(json.dumps({'purpose': 'other', 'txn': 'a' * 16}))
                with self.assertRaisesRegex(broker.BrokerError, 'invalid_restore_hold'):
                    broker.serve(stream)
                self.assertTrue(marker.exists())
                marker.write_text(json.dumps({'purpose': 'restore', 'txn': 'a' * 16}))
                broker.serve(stream)
                self.assertFalse(marker.exists())
                broker.serve(stream)  # idempotent when the response was lost


if __name__ == "__main__":
    unittest.main()
