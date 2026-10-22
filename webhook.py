"""Durable Telegram webhook for Render Free and external PostgreSQL."""
import hmac
import json
import logging
import os
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from app import App

log = logging.getLogger('webhook')


def handler_for(owner, secret, worker):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def answer(self, status, body=b''):
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ('/', '/healthz'):
                return self.answer(404)
            ready = worker.is_alive() and not owner.stopping.is_set()
            try:
                owner.db.rows('SELECT 1')
            except Exception:
                ready = False
            self.answer(200 if ready else 503, b'ok' if ready else b'unavailable')

        def do_POST(self):
            if self.path != '/telegram':
                return self.answer(404)
            if not hmac.compare_digest(self.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), secret):
                return self.answer(403)
            if owner.stopping.is_set():
                return self.answer(503)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    return self.answer(413)
                update = json.loads(self.rfile.read(size))
                if not isinstance(update, dict) or type(update.get('update_id')) is not int:
                    return self.answer(400)
                duplicate = owner.db.rows('SELECT 1 FROM updates WHERE id=?', (update['update_id'],))
                if not duplicate and owner.db.rows("SELECT COUNT(*) n FROM updates WHERE status='pending'")[0]['n'] >= 200:
                    return self.answer(503)
                # Retries are deduplicated. Acknowledge only after durable commit.
                owner.db.ingest([update])
                owner.wakeup.set()
                self.answer(200, b'ok')
            except (ValueError, TypeError, KeyError):
                self.answer(400)
            except Exception as error:
                log.warning('Webhook persistence error: %s', type(error).__name__)
                self.answer(503)

        def log_message(self, *args):
            pass
    return Handler


class WebhookApp(App):
    def run(self):
        if not os.getenv('DATABASE_URL'):
            raise RuntimeError('Webhook requires a persistent DATABASE_URL')
        secret = os.environ['WEBHOOK_SECRET']
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', secret):
            raise ValueError('WEBHOOK_SECRET must contain 32-256 URL-safe characters')
        public = os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL') or ''
        parsed = urlparse(public)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('A public HTTPS URL is required')
        wanted = public.rstrip('/') + '/telegram'
        self.username = self.tg.call('getMe')['username']
        old = self.tg.call('getWebhookInfo').get('url')
        if old and old != wanted:
            raise RuntimeError('Another Telegram webhook is configured')
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        worker = threading.Thread(target=self.web_worker, daemon=True)
        server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), handler_for(self, secret, worker))
        worker.start()
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop(*args):
            self.stopping.set()
            self.wakeup.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            self.tg.call('setWebhook', url=wanted, secret_token=secret,
                         max_connections=1, allowed_updates=['message', 'callback_query'], drop_pending_updates=False)
            self.tg.call('setMyCommands', commands=[
                {'command': 'start', 'description': 'Создать отклик'},
                {'command': 'help', 'description': 'Как пользоваться'},
                {'command': 'usage', 'description': 'Моя статистика'},
                {'command': 'admin', 'description': 'Админка'},
                {'command': 'agreement', 'description': 'Пользовательское соглашение'}])
            log.info('Webhook active for @%s', self.username)
            while not self.stopping.wait(15):
                if not worker.is_alive():
                    raise RuntimeError('Worker stopped')
        finally:
            stop()
            server.shutdown()
            worker.join(timeout=25)
            server.server_close()

    def web_worker(self):
        initialized = False
        pending_finish = None
        cleaned = time.time()
        try:
            while not self.stopping.is_set():
                try:
                    if not initialized:
                        if not self.db.acquire_worker():
                            self.stopping.wait(2)
                            continue
                        self.db.recover()
                        self.db.cleanup()
                        initialized = True
                    if pending_finish is not None:
                        self.db.finish(pending_finish)
                        pending_finish = None
                        continue
                    update = self.db.next_update()
                    if not update:
                        self.wakeup.wait(3)
                        self.wakeup.clear()
                        continue
                    try:
                        self.handle(update)
                    except Exception as error:
                        log.error('Handler failed: %s', type(error).__name__)
                        try:
                            who = (update.get('callback_query') or {}).get('from') or (update.get('message') or {}).get('from')
                            if who:
                                self.send(who['id'], 'Не удалось выполнить действие. Проверьте команду или повторите запрос.')
                        except Exception:
                            pass
                    pending_finish = update['update_id']
                    self.db.finish(pending_finish)
                    pending_finish = None
                    if time.time() - cleaned > 3600:
                        self.db.cleanup()
                        cleaned = time.time()
                except Exception as error:
                    log.error('Worker database error: %s', type(error).__name__)
                    self.stopping.wait(3)
        finally:
            try:
                self.db.release_worker()
            except Exception:
                pass
