import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from db import DB, source_of
from ai import Generator, bounded, presentation

def update(n=1,user=7,text='/start ad_a'):
    return {'update_id':n,'message':{'message_id':n,'chat':{'id':user,'type':'private'},'from':{'id':user,'first_name':'Test'},'text':text}}

class Tests(unittest.TestCase):
    def setUp(self):
        self.db=DB(':memory:')
        self.db.ingest([update()])
    def test_attribution_and_idempotency(self):
        self.db.ingest([update()])
        self.db.ingest([update(2,text='/start ad_b'),update(3,text='Текст')])
        r=self.db.rows('SELECT * FROM users')[0]
        self.assertEqual((r['first_source'],r['last_source']),('ad_a','ad_b'))
        self.assertEqual(len(self.db.rows('SELECT * FROM events')),3)
        self.assertEqual(self.db.get('offset'),'4')
        self.assertIsNone(source_of('/start admin_SECRET'))
    def test_admin_claim(self):
        self.assertFalse(self.db.claim(7,'wrong','secret'))
        self.assertTrue(self.db.claim(7,'secret','secret'))
        self.assertFalse(self.db.claim(8,'secret','secret'))
        self.assertTrue(self.db.admin(7))
    def test_quota_atomic(self):
        for rid in range(1,11):
            self.db.request(rid,7,'text')
        with ThreadPoolExecutor(max_workers=10) as pool:
            results=list(pool.map(lambda rid: self.db.reserve_call(rid,7,3,5),range(1,11)))
        self.assertEqual(sum(x is not None for x in results),3)
    def test_delete_and_copy_ownership(self):
        self.db.request(1,7,'text')
        self.db.result(1,{'kind':'lesson','reply':'Ответ','warning':''})
        self.assertIsNone(self.db.saved_reply(1,8))
        self.assertEqual(self.db.saved_reply(1,7),'Ответ')
        self.db.delete_user(7)
        self.assertEqual(self.db.rows('SELECT * FROM requests'),[])
    def test_formatting(self):
        text,kb=presentation({'kind':'vacancy','reply':'<test> & ответ','warning':'Проверьте стаж'},9)
        self.assertIn('<pre>&lt;test&gt; &amp; ответ</pre>',text)
        self.assertLess(text.index('⚠️'),text.index('<pre>'))
        self.assertEqual(kb[0][0]['callback_data'],'alt:9')
        self.assertEqual(kb[0][1]['callback_data'],'emoji:9')
        text,kb=presentation({'kind':'lesson','reply':'а'*400,'warning':''},9)
        self.assertEqual(kb[0][0]['callback_data'],'alt:9')
        self.assertLessEqual(len(bounded('Длинное слово '*100)),500)
    def test_request_limit_counts_requests_not_repair_calls(self):
        self.db.request(10,7,'text')
        self.assertIsNotNone(self.db.reserve_call(10,7,1,5))
        self.assertIsNotNone(self.db.reserve_call(10,7,1,5))
        self.db.request(11,7,'text')
        self.assertIsNone(self.db.reserve_call(11,7,1,5))

    def test_variant_input_ownership_cleanup_and_deletion(self):
        import time
        self.db.request(1,7,'image')
        self.db.save_input(1, {'photo':[{'file_id':'abc'}], 'caption':'ОГЭ'})
        self.db.result(1,{'kind':'lesson','reply':'Ответ','warning':''})
        self.assertIsNone(self.db.saved_input(1,8))
        self.assertEqual(self.db.saved_input(1,7)['caption'],'ОГЭ')
        with self.db.tx() as c:
            c.execute('UPDATE requests SET created=? WHERE id=1',(time.time()-8*86400,))
        self.db.cleanup()
        self.assertIsNone(self.db.saved_input(1,7))
        self.assertEqual(self.db.rows('SELECT * FROM request_inputs'),[])

    def test_generator_repair_and_image(self):
        results=[{'kind':'lesson','reply':'слово '*130,'warning':''},{'kind':'lesson','reply':'Здравствуйте! Какие темы сейчас сложнее всего?','warning':''}]
        calls=[]
        def fake(url,payload,headers,service):
            calls.append(payload)
            return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(results.pop(0))}]}],'usage':{'input_tokens':10}}
        with patch('ai.http_json',fake):
            result=Generator('fake',prompt='Rules').generate('Заказ',[(b'abc','image/png')],'Имя: Константин',lambda:1,lambda *a:None)
        self.assertLessEqual(len(result['reply']),500)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0]['input'][0]['content'][1]['type'],'input_image')
        self.assertFalse(calls[0]['store'])
    def test_recovery(self):
        self.db.next_update()
        self.db.request(1,7,'text')
        self.db.recover()
        self.assertIsNotNone(self.db.next_update())
        self.assertEqual(self.db.rows('SELECT status FROM requests')[0]['status'],'interrupted')

class HandlerTests(unittest.TestCase):
    def setUp(self):
        from app import App
        from unittest.mock import Mock
        self.app=App.__new__(App)
        self.app.db=DB(':memory:')
        self.app.tg=Mock()
        self.app.ai=Mock()
        self.app.profile='Константин'
        self.app.claim_code='secret'
        self.app.user_limit=60
        self.app.global_limit=300
        self.app.username='testbot'
    def feed(self,u):
        self.app.db.ingest([u])
        self.app.handle(u)
    def test_text_and_vacancy(self):
        self.app.ai.generate.return_value={'kind':'vacancy','reply':'Здравствуйте! Какой график занятий?','warning':'Стаж неизвестен.'}
        self.feed(update(text='Школа ищет преподавателя'))
        call=self.app.tg.send.call_args
        self.assertIn('<pre>',call.args[1])
        self.assertIn('⚠️',call.args[1])
        self.assertEqual(self.app.db.counts(7)['success'],1)
    def test_photo(self):
        u=update(text='')
        u['message']['photo']=[{'file_id':'photo-id'}]
        self.app.tg.file.return_value=(b'PNG','image/png')
        self.app.ai.generate.return_value={'kind':'lesson','reply':'Здравствуйте! Какие темы сложны?','warning':''}
        self.feed(u)
        self.assertEqual(self.app.ai.generate.call_args.args[1],[(b'PNG','image/png')])
    def test_alternative_and_emoji_use_original_ad(self):
        self.app.ai.generate.return_value={'kind':'lesson','reply':'Первый ответ','warning':''}
        self.feed(update(text='ОГЭ информатика, 9 класс'))
        for n,style in [(2,'alt'),(3,'emoji')]:
            cb={'update_id':n,'callback_query':{'id':str(n),'from':{'id':7},'data':f'{style}:1','message':{'message_id':99,'chat':{'id':7,'type':'private'},'from':{'id':123,'is_bot':True},'text':'Первый ответ'}}}
            self.feed(cb)
            call=self.app.ai.generate.call_args
            self.assertEqual(call.args[0],'ОГЭ информатика, 9 класс')
            self.assertEqual(call.kwargs['style'],style)
            self.assertEqual(call.kwargs['previous'],'Первый ответ')
        self.assertEqual(self.app.db.counts(7)['success'],3)

    def test_variant_cannot_read_another_users_ad(self):
        self.app.ai.generate.return_value={'kind':'lesson','reply':'Ответ','warning':''}
        self.feed(update(text='ОГЭ'))
        cb={'update_id':2,'callback_query':{'id':'cb','from':{'id':8},'data':'alt:1','message':{'message_id':99,'chat':{'id':8,'type':'private'},'from':{'id':123},'text':'Ответ'}}}
        self.feed(cb)
        self.assertEqual(self.app.ai.generate.call_count,1)

    def test_start_and_agreement(self):
        self.feed(update(text='/start'))
        greeting=self.app.tg.send.call_args.args[1]
        self.assertIn('принимаете пользовательское соглашение',greeting)
        self.assertNotIn('/delete_data',greeting)
        self.assertNotIn('/privacy',greeting)
        self.feed(update(2,text='/agreement'))
        self.assertIn('/delete_data',self.app.tg.send.call_args.args[1])

    def test_admin_acl(self):
        self.feed(update(text='/export'))
        self.app.tg.document.assert_not_called()
        self.assertIn('только администратору',self.app.tg.send.call_args.args[1])
    def test_duplicate_no_second_generation(self):
        self.app.ai.generate.return_value={'kind':'lesson','reply':'Ответ','warning':''}
        self.feed(update(text='ОГЭ'))
        self.app.handle(update(text='ОГЭ'))
        self.assertEqual(self.app.ai.generate.call_count,1)

if __name__=='__main__':unittest.main()
