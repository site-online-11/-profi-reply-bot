import csv
import html
import io
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from ai import Generator, presentation
from db import DB
from transport import Telegram, APIError

log = logging.getLogger('bot')
HELP = ('Пришлите текст объявления или скриншот (фото либо PNG/JPEG/WebP до 10 МБ). '
        'Я подготовлю персональный отклик до 500 символов. Если это найм, предупрежу и всё равно предложу ответ.\n\n'
        'Готовый текст сразу появится в блоке для копирования. Под ним можно выбрать другой вариант или вариант со смайликами.\n'
        '/usage — ваши генерации\n/agreement — пользовательское соглашение')
START = 'Нажимая «Начать» или продолжая пользоваться ботом, вы принимаете пользовательское соглашение: /agreement.\n\n' + HELP
AGREEMENT = Path(__file__).with_name('agreement.txt').read_text()


class App:
    def __init__(self):
        if os.getenv('DATABASE_URL'):
            from db_pg import DB as PostgresDB
            self.db = PostgresDB()
        else:
            self.db = DB(os.getenv('DB_PATH', 'data/bot.sqlite3'))
        self.tg = Telegram(os.environ['BOT_TOKEN'])
        self.ai = Generator(os.environ['OPENAI_API_KEY'], os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'))
        self.default_prompt = self.ai.prompt
        self.claim_code = os.getenv('ADMIN_CLAIM_CODE', '')
        self.user_limit = int(os.getenv('USER_DAILY_LIMIT', '60'))
        self.global_limit = int(os.getenv('GLOBAL_DAILY_LIMIT', '300'))
        self.profile = Path(__file__).with_name('profile.txt').read_text()
        self.last_poll = time.time()
        self.worker_alive = True
        for uid in os.getenv('ADMIN_IDS', '').split(','):
            if uid.strip().isdigit():
                with self.db.tx() as c:
                    c.execute('INSERT OR IGNORE INTO admins VALUES(?)', (int(uid),))

    def send(self, uid, text, keyboard=None, formatted=False):
        return self.tg.send(uid, text, keyboard, formatted)

    def admin_panel(self, uid, cmd, arg=''):
        if not self.db.admin(uid):
            self.send(uid, 'Доступ только администратору.')
            return
        if cmd in ('/prompt', '/edit_prompt', '/set_prompt', '/reset_prompt', '/confirm_reset_prompt', '/cancel'):
            self.prompt_admin(uid, cmd, arg)
            return
        if cmd in ('/admin', '/stats'):
            n = self.db.rows('SELECT COUNT(*) n FROM users')[0]['n']
            c = self.db.counts()
            self.send(uid, f"Пользователи: {n}\nЗапросы: {c['requests']}\nУспешно: {c['success']}\nОшибки: {c['errors']}\nВакансии: {c['vacancies']}\nВызовы ИИ: {c['api_calls']}\nТокены вход/выход: {c['input_tokens']}/{c['output_tokens']}\n\n/users — пользователи\n/user ID — карточка\n/sources — источники\n/source название — рекламная ссылка\n/export — CSV\n/profile — профиль преподавателя\n/set_profile текст — заменить профиль\n/prompt — системный промпт\n/block ID, /unblock ID", [[{'text':'Пользователи','callback_data':'admin:users:0'},{'text':'Источники','callback_data':'admin:sources'}], [{'text':'Системный промпт','callback_data':'admin:prompt'}]])
        elif cmd == '/users':
            page = max(0, int(arg or '0'))
            rows = self.db.rows('SELECT * FROM users ORDER BY created DESC LIMIT 10 OFFSET ?', (page*10,))
            lines = [f"{r['id']} | @{r['username'] or '—'} | {(r['first_name'] or '')[:40]} | {r['first_source']} | ответов: {self.db.counts(r['id'])['success']}" for r in rows]
            self.send(uid, '\n'.join(lines) or 'Нет пользователей.', [[{'text':'←','callback_data':f'admin:users:{max(0,page-1)}'},{'text':'→','callback_data':f'admin:users:{page+1}'}]])
        elif cmd == '/user':
            rows = self.db.rows('SELECT * FROM users WHERE id=?', (int(arg),))
            self.send(uid, json.dumps({'user': rows[0] if rows else None, 'usage':self.db.counts(int(arg))}, ensure_ascii=False, indent=2))
        elif cmd == '/sources':
            rows = self.db.rows('SELECT first_source source,COUNT(*) users FROM users GROUP BY first_source ORDER BY users DESC LIMIT 50')
            self.send(uid, 'Первый источник:\n'+'\n'.join(f"{r['source']}: {r['users']}" for r in rows))
        elif cmd == '/source':
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', arg) or arg.startswith('admin_'):
                self.send(uid, 'Название: 1–64 латинских букв, цифр, _ или -. Например: /source insta_bio')
            else:
                self.send(uid, f'https://t.me/{self.username}?start={arg}')
        elif cmd == '/profile':
            self.send(uid, self.db.get('profile', self.profile))
        elif cmd == '/set_profile':
            if not arg or len(arg)>3000:
                self.send(uid, 'Укажите полный профиль до 3000 символов после /set_profile.')
            else:
                self.db.set('profile',arg)
                self.send(uid, 'Профиль обновлён.')
        elif cmd in ('/block','/unblock'):
            with self.db.tx() as c:
                c.execute('UPDATE users SET blocked=? WHERE id=?',(int(cmd=='/block'),int(arg)))
            self.send(uid, 'Готово.')
        elif cmd == '/export':
            rows = self.db.rows('''SELECT u.*,COUNT(r.id) requests,COALESCE(SUM(CASE WHEN r.status='success' THEN 1 ELSE 0 END),0) success FROM users u LEFT JOIN requests r ON r.user_id=u.id GROUP BY u.id ORDER BY u.created DESC''')
            out = io.StringIO()
            if rows:
                writer = csv.DictWriter(out,fieldnames=list(rows[0]))
                writer.writeheader()
                for r in rows:
                    # Neutralize spreadsheet formula injection from Telegram names.
                    writer.writerow({k: ("'"+v if isinstance(v,str) and v[:1] in ('=','+','-','@','\t','\r') else v) for k,v in r.items()})
            self.tg.document(uid,'users.csv',('\ufeff'+out.getvalue()).encode())

    def prompt_admin(self, uid, cmd, arg=''):
        # This method is also used by the message editor: check permissions here too.
        if not self.db.admin(uid):
            self.send(uid, 'Доступ только администратору.')
            return
        edit_key = f'prompt_edit:{uid}'
        buttons = [[{'text':'Изменить промпт','callback_data':'admin:edit_prompt'}],
                   [{'text':'Сбросить к исходному','callback_data':'admin:reset_prompt'}]]
        if cmd == '/prompt':
            current = self.db.get('system_prompt', self.default_prompt)
            self.tg.document(uid, 'system-prompt.txt', current.encode('utf-8'))
            # Keep each block below Telegram's UTF-16 message limit, including emoji.
            if len(current) <= 1500:
                self.send(uid, '<pre>' + html.escape(current) + '</pre>', formatted=True)
            self.send(uid, 'Это текущий системный промпт. Изменения применяются к следующим откликам без перезапуска. Лимит ответа — 500 символов.', buttons)
        elif cmd == '/edit_prompt' or (cmd == '/set_prompt' and not arg):
            self.db.set(edit_key, time.time() + 1800)
            self.send(uid, 'Пришлите новый промпт одним текстовым сообщением или файлом .txt (до 20 000 символов). Я сразу сохраню его. Ожидание — 30 минут. Отмена: /cancel.', [[{'text':'Отмена','callback_data':'admin:cancel'}]])
        elif cmd == '/cancel':
            self.db.set(edit_key, '0')
            self.send(uid, 'Редактирование отменено. Промпт не изменён.')
        elif cmd == '/reset_prompt':
            self.send(uid, 'Вернуть исходный промпт из кода бота?', [[{'text':'Да, сбросить','callback_data':'admin:confirm_reset_prompt'}, {'text':'Отмена','callback_data':'admin:cancel'}]])
        elif cmd == '/confirm_reset_prompt':
            self.save_prompt(uid, self.default_prompt)
        elif cmd == '/set_prompt':
            self.save_prompt(uid, arg)

    def save_prompt(self, uid, text):
        if not self.db.admin(uid):
            self.send(uid, 'Доступ только администратору.')
            return
        text = text.strip()
        if not text or len(text) > 20000:
            self.send(uid, 'Промпт должен содержать от 1 до 20 000 символов. Пришлите его снова или нажмите /cancel.')
            return
        with self.db.tx() as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('system_prompt', text))
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (f'prompt_edit:{uid}', '0'))
        self.send(uid, f'Промпт сохранён ({len(text)} символов). Он применяется ко всем следующим откликам, включая варианты. Лимит ответа — 500 символов.', [[{'text':'Посмотреть промпт','callback_data':'admin:prompt'}]])

    def handle(self, u):
        cb = u.get('callback_query')
        m = u.get('message') or cb['message']
        uid = int((cb or {}).get('from', m.get('from'))['id'])
        if cb:
            data = cb.get('data','')
            self.tg.call('answerCallbackQuery',callback_query_id=cb['id'])
            if data == 'agreement':
                self.send(uid, AGREEMENT)
            elif data.startswith(('alt:', 'emoji:')):
                try:
                    style, original = data.split(':', 1)
                    original = int(original)
                except ValueError:
                    return
                saved = self.db.saved_input(original, uid)
                previous = self.db.saved_reply(original, uid)
                if not saved:
                    self.send(uid, 'Пришлите объявление ещё раз, чтобы создать новый вариант.')
                    return
                message = {'chat': m['chat'], 'from': cb['from'], **saved}
                self.handle({'update_id': u['update_id'], 'message': message, 'variant': style, 'previous_reply': previous})
            elif data.startswith('copy:'):
                reply = self.db.saved_reply(int(data.split(':')[1]),uid)
                self.send(uid, '<pre>'+html.escape(reply)+'</pre>' if reply else 'Ответ уже удалён. Пришлите объявление ещё раз.', formatted=bool(reply))
            elif data == 'delete:yes':
                self.db.delete_user(uid)
                self.send(uid,'Ваши данные удалены. Новое обращение создаст новую запись.')
            elif data.startswith('admin:'):
                parts=data.split(':')
                self.admin_panel(uid,'/'+parts[1],parts[2] if len(parts)>2 else '')
            return
        text=m.get('text','')
        editing = float(self.db.get(f'prompt_edit:{uid}', '0')) > time.time()
        if editing and not text.startswith('/') and self.db.admin(uid):
            if m.get('document'):
                try:
                    text = self.tg.text_file(m['document'])
                except (ValueError, APIError):
                    self.send(uid, 'Не удалось прочитать файл. Пришлите .txt в UTF-8 до 80 КБ или текст одним сообщением. Отмена: /cancel.')
                    return
            elif not text:
                self.send(uid, 'Ожидаю текст промпта или файл .txt. Чтобы отправить объявление, нажмите /cancel.')
                return
            self.save_prompt(uid, text)
            return
        if text.startswith('/'):
            parts=text.split(maxsplit=1)
            cmd=parts[0].split('@')[0].lower()
            arg=parts[1].strip() if len(parts)>1 else ''
            if cmd=='/start' and arg.startswith('admin_'):
                claimed=self.db.claim(uid,arg[6:],self.claim_code)
                self.send(uid,'Вы администратор. Откройте /admin.' if claimed else 'Ссылка администратора недействительна или уже использована.')
            elif cmd=='/start':
                self.send(uid,START,[[{'text':'Пользовательское соглашение','callback_data':'agreement'}]])
            elif cmd=='/help':
                self.send(uid,HELP)
            elif cmd in ('/agreement','/privacy'): self.send(uid,AGREEMENT)
            elif cmd=='/usage':
                c=self.db.counts(uid)
                self.send(uid,f"Запросов: {c['requests']}\nОтветов: {c['success']}\nВызовов ИИ: {c['api_calls']}\nДневной лимит: {self.user_limit} запросов (включая варианты).")
            elif cmd=='/delete_data':
                self.send(uid,'Удалить ваши данные и сохранённые ответы?',[[{'text':'Да, удалить','callback_data':'delete:yes'}]])
            elif cmd in ('/admin','/stats','/users','/user','/sources','/source','/export','/profile','/set_profile','/block','/unblock','/prompt','/edit_prompt','/set_prompt','/reset_prompt','/cancel'):
                self.admin_panel(uid,cmd,arg)
            else: self.send(uid,'Неизвестная команда. /help')
            return
        blocked=self.db.rows('SELECT blocked FROM users WHERE id=?',(uid,))
        if blocked and blocked[0]['blocked']:
            self.send(uid,'Доступ к генерации ограничен администратором.')
            return
        rid=u['update_id']
        old=self.db.rows('SELECT * FROM requests WHERE id=?',(rid,))
        if old:
            if old[0]['status']=='generated':
                r=old[0]
                body,kb=presentation({'kind':r['classification'],'reply':r['reply'],'warning':r['warning']},rid)
                self.send(uid,body,kb,True)
                self.db.delivered(rid)
            elif old[0]['status']=='interrupted':
                self.send(uid,'Запрос прервался при перезапуске. Пришлите объявление ещё раз.')
            return
        photo=m.get('photo')
        document=m.get('document')
        if not text and not photo and not document:
            self.send(uid,'Пришлите текст объявления или изображение.')
            return
        if len(text)>16000:
            self.send(uid,'Сократите объявление до 16 000 символов.')
            return
        if not self.db.request(rid,uid,'image' if photo or document else 'text'):
            return
        self.db.save_input(rid, m)
        try:
            images=[]
            if photo or document:
                file=(photo[-1] if photo else document)
                if file.get('file_size',0)>10*1024*1024:
                    raise ValueError('Изображение больше 10 МБ. Пришлите его как фото.')
                images=[self.tg.file(file['file_id'])]
            self.tg.call('sendChatAction',chat_id=uid,action='typing')
            self.ai.prompt = self.db.get('system_prompt', self.default_prompt)
            result=self.ai.generate(text or m.get('caption',''),images,self.db.get('profile',self.profile),lambda:self.db.reserve_call(rid,uid,self.user_limit,self.global_limit),self.db.call_done,style=u.get('variant'),previous=u.get('previous_reply'))
            self.db.result(rid,result)
            body,kb=presentation(result,rid)
            self.send(uid,body,kb,True)
            if result['reply']: self.db.delivered(rid)
            else: self.db.fail(rid,'unreadable')
        except APIError as e:
            self.db.fail(rid,e.service+':'+str(e.status))
            msg='Сервис ИИ временно недоступен. Попробуйте позже.'
            if e.service=='openai' and e.status in (401,403): msg='Нужно проверить доступ к ИИ. Администратор может посмотреть статус в /admin.'
            elif e.status==429: msg='Достигнут лимит сервиса. Попробуйте позже.'
            self.send(uid,msg)
            log.warning('Request failed service=%s status=%s',e.service,e.status)
        except ValueError as e:
            self.db.fail(rid,'input_or_generation')
            # Only known, localized messages may be shown; JSON parser errors never leak input.
            msg=str(e)
            self.send(uid,msg if msg and 'А'<=msg[0]<='я' else 'Не удалось обработать объявление. Пришлите его ещё раз.')

    def worker(self):
        while True:
            u=self.db.next_update()
            if not u:
                time.sleep(.3)
                continue
            try: self.handle(u)
            except Exception as e:
                log.error('Handler error: %s',type(e).__name__)
                try:
                    who=(u.get('callback_query') or {}).get('from') or (u.get('message') or {}).get('from')
                    if who: self.send(who['id'],'Не удалось выполнить действие. Проверьте команду или повторите запрос.')
                except Exception: pass
            finally: self.db.finish(u['update_id'])

    def run(self):
        self.username=self.tg.call('getMe')['username']
        if self.tg.call('getWebhookInfo').get('url'):
            raise RuntimeError('Existing webhook found. Stop the previous deployment before switching to polling.')
        self.db.recover()
        self.db.cleanup()
        self.tg.call('setMyCommands',commands=[{'command':'start','description':'Создать отклик'},{'command':'help','description':'Как пользоваться'},{'command':'usage','description':'Моя статистика'},{'command':'admin','description':'Админка'},{'command':'agreement','description':'Пользовательское соглашение'}])
        worker=threading.Thread(target=self.worker,daemon=True)
        worker.start()
        app=self
        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                healthy=worker.is_alive() and time.time()-app.last_poll<120
                self.send_response(200 if healthy else 503)
                self.end_headers()
                self.wfile.write(b'ok' if healthy else b'unhealthy')
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','8080'))),Health)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        log.info('Bot started @%s',self.username)
        cleaned=time.time()
        while worker.is_alive():
            try:
                if self.db.rows("SELECT COUNT(*) n FROM updates WHERE status='pending'")[0]['n']>200:
                    time.sleep(1)
                    continue
                updates=self.tg.call('getUpdates',offset=int(self.db.get('offset','0')),timeout=25,allowed_updates=['message','callback_query'])
                self.db.ingest(updates)
                self.last_poll=time.time()
                if time.time()-cleaned>3600:
                    self.db.cleanup()
                    cleaned=time.time()
            except APIError as e:
                log.warning('Polling failed status=%s',e.status)
                if e.status in (401,409): raise RuntimeError('Invalid token or another polling instance is active.') from None
                time.sleep(min(max(e.retry_after,3),30))
        raise RuntimeError('Worker stopped')

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    # For local launch only; hosted deployment uses environment variables.
    if os.getenv('SECRETS_FILE'):
        for k,v in json.loads(Path(os.environ['SECRETS_FILE']).read_text()).items():
            os.environ.setdefault(k,str(v))
    try:
        mode = os.getenv('BOT_MODE', 'webhook' if os.getenv('DATABASE_URL') else 'polling')
        if mode == 'webhook':
            from webhook import WebhookApp
            WebhookApp().run()
        elif mode == 'polling':
            App().run()
        else:
            raise ValueError('Unknown BOT_MODE')
    except Exception as e:
        log.error('Startup stopped: %s',type(e).__name__)
        raise SystemExit(1)

