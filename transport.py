"""HTTP clients: never expose URLs containing credentials in exceptions or logs."""
import json
import time
import urllib.error
import urllib.request
import uuid


class APIError(Exception):
    def __init__(self, service, status=0, code='network', retry_after=0):
        self.service, self.status, self.code, self.retry_after = service, status, code, retry_after
        super().__init__(f'{service}:{status}:{code}')


def http_json(url, data=None, headers=None, timeout=100, service='api'):
    h = {'Content-Type': 'application/json', **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read(8192)); error = d.get('error', {})
            code = error.get('code') if isinstance(error, dict) else None
            retry = d.get('parameters', {}).get('retry_after', 0)
        except Exception:
            code, retry = None, 0
        raise APIError(service, e.code, str(code or 'http_error'), retry) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise APIError(service) from None


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **data):
        r = http_json(f'https://api.telegram.org/bot{self.token}/{method}', data, service='telegram', timeout=45)
        if not r.get('ok'):
            raise APIError('telegram', r.get('error_code', 0), 'rejected', r.get('parameters', {}).get('retry_after', 0))
        return r['result']

    def send(self, chat_id, text, keyboard=None, html=False):
        data = {'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}
        if html:
            data['parse_mode'] = 'HTML'
        if keyboard:
            data['reply_markup'] = {'inline_keyboard': keyboard}
        return self.call('sendMessage', **data)

    def file(self, file_id, max_bytes=10*1024*1024):
        f = self.call('getFile', file_id=file_id)
        if f.get('file_size', 0) > max_bytes:
            raise ValueError('Изображение больше 10 МБ. Пришлите его как фото.')
        path = f.get('file_path', '')
        if not path or '://' in path or '..' in path:
            raise ValueError('Не удалось получить изображение.')
        try:
            with urllib.request.urlopen(f'https://api.telegram.org/file/bot{self.token}/{path}', timeout=30) as r:
                raw = r.read(max_bytes+1)
        except (OSError, urllib.error.URLError):
            raise APIError('telegram') from None
        if len(raw) > max_bytes:
            raise ValueError('Изображение больше 10 МБ. Пришлите его как фото.')
        if raw.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        elif raw.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            mime = 'image/webp'
        else:
            raise ValueError('Поддерживаются JPG, PNG и WEBP. HEIC отправьте как обычное фото.')
        return raw, mime

    def text_file(self, document):
        if not document.get('file_name', '').lower().endswith('.txt'):
            raise ValueError('Expected a .txt file')
        limit = 80000
        if document.get('file_size', 0) > limit:
            raise ValueError('File too large')
        info = self.call('getFile', file_id=document['file_id'])
        path = info.get('file_path', '')
        if not path or '://' in path or '..' in path or info.get('file_size', 0) > limit:
            raise ValueError('Invalid file')
        try:
            with urllib.request.urlopen(f'https://api.telegram.org/file/bot{self.token}/{path}', timeout=30) as response:
                raw = response.read(limit + 1)
        except (OSError, urllib.error.URLError):
            raise APIError('telegram') from None
        if len(raw) > limit:
            raise ValueError('File too large')
        try:
            return raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Expected UTF-8') from None

    def document(self, chat_id, filename, raw):
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(f'https://api.telegram.org/bot{self.token}/sendDocument', data=body,
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.load(r)
                if not result.get('ok'):
                    raise APIError('telegram', result.get('error_code', 0))
                return result['result']
        except (urllib.error.URLError, OSError):
            raise APIError('telegram') from None

