"""Integration tests. TEST_DATABASE_URL must point to a disposable database."""
import os
import unittest
from unittest.mock import patch
import test_bot
from db_pg import DB


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL is not set')
class PostgresTests(test_bot.Tests):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'DATABASE_URL': os.environ['TEST_DATABASE_URL']})
        self.environment.start()
        self.db = DB()
        # This test database is dedicated to the bot and explicitly disposable.
        self.db.c.execute('TRUNCATE users,events,updates,requests,api_calls,settings,admins RESTART IDENTITY CASCADE')
        self.db.ingest([test_bot.update()])

    def tearDown(self):
        self.db.c.close()
        self.environment.stop()

    def test_export_and_cleanup(self):
        self.db.request(1, 7, 'text')
        self.db.result(1, {'kind': 'lesson', 'reply': 'Ответ', 'warning': ''})
        self.db.delivered(1)
        rows = self.db.rows("SELECT u.*,COUNT(r.id) requests,COALESCE(SUM(CASE WHEN r.status='success' THEN 1 ELSE 0 END),0) success FROM users u LEFT JOIN requests r ON r.user_id=u.id GROUP BY u.id")
        self.assertEqual(rows[0]['success'], 1)
        self.db.cleanup()

    def test_reconnect_and_worker_ownership(self):
        self.assertTrue(self.db.acquire_worker())
        self.db.release_worker()
        self.db.c.close()
        self.assertEqual(self.db.get('offset'), '2')


if __name__ == '__main__':
    unittest.main()

