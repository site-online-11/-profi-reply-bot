import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from db import DB
from test_bot import update
from webhook import handler_for


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        self.owner = SimpleNamespace(db=self.db, stopping=threading.Event(), wakeup=threading.Event())
        worker = Mock()
        worker.is_alive.return_value = True
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.owner, 'test-secret', worker))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.db.c.close()

    def post(self, body, secret='test-secret'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request('POST', '/telegram', json.dumps(body), {'X-Telegram-Bot-Api-Secret-Token': secret})
        response = conn.getresponse()
        status = response.status
        response.read()
        conn.close()
        return status

    def test_auth_and_invalid_payload(self):
        self.assertEqual(self.post(update(), 'wrong'), 403)
        self.assertEqual(self.post({'update_id': True}), 400)
        self.assertEqual(self.db.rows('SELECT * FROM updates'), [])

    def test_durable_retry_and_out_of_order(self):
        self.assertEqual(self.post(update(12)), 200)
        self.assertEqual(self.post(update(10)), 200)
        self.assertEqual(self.post(update(12)), 200)
        self.assertEqual(len(self.db.rows('SELECT * FROM updates')), 2)
        self.assertEqual(len(self.db.rows('SELECT * FROM events')), 2)
        self.assertTrue(self.owner.wakeup.is_set())

    def test_failed_commit_is_not_acknowledged(self):
        with patch.object(self.db, 'ingest', side_effect=RuntimeError('db unavailable')):
            self.assertEqual(self.post(update()), 503)
        self.assertEqual(self.db.rows('SELECT * FROM updates'), [])

    def test_shutdown_defers_delivery(self):
        self.owner.stopping.set()
        self.assertEqual(self.post(update()), 503)


if __name__ == '__main__':
    unittest.main()

