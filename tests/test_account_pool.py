import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from core import converter
from admin.server import Store
from admin.pool import AccountPool, PoolMiddleware, REQUEST_CREDENTIAL, summarize_packages, request_affinity, billing_host
from test_admin_server import credential


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root/'management', root/'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.now = 1800000000
        self.checked = False
        self.checkin_posts = 0
        self.fail = False
        self.pages = []
        def respond(req):
            if self.fail:
                return httpx.Response(500)
            if req.url.path.endswith('checkin-activity-status'):
                return httpx.Response(200,json={'code':0,'data':{'active':True,'today_checked_in':self.checked}})
            if req.url.path.endswith('daily-checkin'):
                self.checkin_posts += 1; self.checked=True
                return httpx.Response(200,json={'code':0,'data':{}})
            body=json.loads(req.content)
            self.pages.append(body['PageNumber'])
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':1,'Accounts':[{'CycleCapacityRemainPrecise':'479.59','CycleCapacitySize':500,'CapacityRemain':500}]}}}})
        self.pool=AccountPool(self.store,clock=lambda:self.now,client_factory=lambda:httpx.Client(transport=httpx.MockTransport(respond)))
        for aid,item in self.store.data['accounts'].items():
            self.store.manager_for(aid,item).get_headers=Mock(return_value={'X-Domain':'www.codebuddy.cn'})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def test_precise_cycle_balance_and_unknown(self):
        result=summarize_packages([{'CapacityRemain':500,'CapacitySize':500,'CycleCapacityRemainPrecise':'479.59','CycleCapacitySizePrecise':'500'},{'CapacityRemainPrecise':'0.12','CapacitySize':100}])
        self.assertEqual(result['remaining'],479.71)
        with self.assertRaises(ValueError): summarize_packages([{}])
        self.assertEqual(self.pool.state(self.store.account_rows()[0]),'available')

    def test_round_robin_paused_cooling_exhausted_and_manual(self):
        self.assertEqual([self.pool.select()[0] for _ in range(4)],[self.a,self.b,self.a,self.b])
        self.store.data['accounts'][self.a]['enabled']=False
        self.assertEqual(self.pool.select()[0],self.b)
        self.pool.record(self.b,429)
        with self.assertRaises(HTTPException):self.pool.select()
        self.now+=1801
        self.assertEqual(self.pool.select()[0],self.b)
        self.pool.update(self.b,remaining=0,packages=1,credits_updated=self.now)
        with self.assertRaises(HTTPException):self.pool.select()
        self.now+=601
        self.assertEqual(self.pool.select()[0],self.b)
        self.store.data['pool']['routing']='manual';self.store.data['active']=self.a
        with self.assertRaises(HTTPException):self.pool.select()

    def test_affinity_failover_persistence_expiry_and_manual(self):
        key = request_affinity({b'authorization': b'Bearer api-test'}, {'model': 'm', 'prompt_cache_key': 'session-one'})
        self.assertEqual([self.pool.select(key)[0] for _ in range(3)], [self.a] * 3)
        self.pool.record(self.a)
        self.assertEqual(AccountPool(self.store, clock=lambda: self.now).select(key)[0], self.a)
        self.pool.record(self.a, 429)
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.now += 1801
        self.assertEqual(self.pool.select(key)[0], self.b)
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.a
        self.assertEqual(self.pool.select(key)[0], self.a)
        self.now += 86401
        self.pool.select()
        self.assertNotIn(key, self.store.data['session_bindings'])

    def test_affinity_is_scoped_to_caller_model_and_session(self):
        headers = {b'authorization': b'Bearer one'}
        body = {'model': 'm', 'prompt_cache_key': 'secret-session'}
        key = request_affinity(headers, body)
        self.assertEqual(len(key), 64)
        self.assertNotEqual(key, request_affinity({b'authorization': b'Bearer two'}, body))
        self.assertNotEqual(key, request_affinity(headers, dict(body, model='other')))
        self.assertEqual(key, request_affinity(headers, dict(body, input='next question')))
        self.assertEqual(request_affinity(headers, {'input': [{'role': 'user', 'content': 'first'}]}),
                         request_affinity(headers, {'input': [{'role': 'user', 'content': 'first'}, {'role': 'user', 'content': 'next'}]}))

    async def test_middleware_replays_body_and_reuses_session_account(self):
        seen = []
        async def app(scope, receive, send):
            message = await receive()
            seen.append((REQUEST_CREDENTIAL.get().path.name, json.loads(message['body'])))
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            bodies = [{'prompt_cache_key': 'same', 'input': 'first'}, {'prompt_cache_key': 'same', 'input': 'next'}]
            for body in bodies:
                response = await client.post('/v1/responses', json=body, headers={'Authorization': 'Bearer api-test'})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual([item[1] for item in seen], bodies)

    async def test_responses_failed_event_cools_account(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"429"}}}\n\n'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)), base_url='http://test') as client:
            await client.post('/v1/responses', json={'input': 'hello'}, headers={'Authorization': 'Bearer api-test'})
        self.assertGreater(self.store.data['account_status'][self.a]['cooldown_until'], self.now)

    async def test_checkin_idempotent_and_failure_retains_balance(self):
        a,b=await asyncio.gather(asyncio.to_thread(self.pool.operate,self.a,'checkin'),asyncio.to_thread(self.pool.operate,self.a,'checkin'))
        self.assertTrue(a['ok'] and b['ok']);self.assertEqual(self.checkin_posts,1)
        self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)
        self.fail=True
        result=await asyncio.to_thread(self.pool.operate,self.a,'status')
        self.assertFalse(result['ok']);self.assertEqual(self.store.data['account_status'][self.a]['remaining'],479.59)

    async def test_paused_excluded_from_batch_and_schedule_persists(self):
        self.store.data['accounts'][self.b]['enabled']=False
        self.store.data['pool']['checkin_time']='00:00'
        await self.pool.tick()
        self.assertEqual(self.checkin_posts,1)
        self.assertNotIn(self.b,self.store.data['account_status'])
        restarted=AccountPool(self.store,clock=lambda:self.now,client_factory=self.pool.client_factory)
        await restarted.tick()
        self.assertEqual(self.checkin_posts,1)
        data=json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertTrue(data['account_status'][self.a]['checkin_date'])

    async def test_context_isolation_concurrent_requests_and_auth(self):
        async def app(scope,receive,send):
            before=REQUEST_CREDENTIAL.get().path.name
            await asyncio.sleep(.01)
            after=REQUEST_CREDENTIAL.get().path.name
            data=json.dumps({'before':before,'after':after}).encode()
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':data})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app,self.pool)),base_url='http://test') as client:
            self.assertEqual((await client.post('/v1/responses')).status_code,401)
            results=await asyncio.gather(*(client.post('/v1/responses',headers={'Authorization':'Bearer api-test'}) for _ in range(4)))
            files=[r.json()['before'] for r in results]
            self.assertEqual(len(set(files)),2)
            self.assertTrue(all(r.json()['before']==r.json()['after'] for r in results))
        self.assertIsNone(REQUEST_CREDENTIAL.get())

    async def test_sse_error_marks_cooldown_without_replay(self):
        calls=[]
        async def app(scope,receive,send):
            calls.append(1)
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            for part in [b'data: {"error":',b'{"code":429}}\n\n']:
                await send({'type':'http.response.body','body':part,'more_body':True})
            await send({'type':'http.response.body','body':b''})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app,self.pool)),base_url='http://test') as client:
            response=await client.post('/v1/messages',headers={'Authorization':'Bearer api-test'})
            self.assertEqual(response.status_code,200)
        self.assertEqual(len(calls),1)
        self.assertGreater(self.store.data['account_status'][self.a]['cooldown_until'],self.now)
        self.assertEqual(self.pool.select()[0],self.b)

    def test_pagination_is_complete(self):
        def handler(req):
            page=json.loads(req.content)['PageNumber']
            accounts=[{'CapacityRemainPrecise':'1.25','CapacitySize':2}]* (100 if page==1 else 1)
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':101,'Accounts':accounts}}}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            result=self.pool.credits(c,{})
        self.assertEqual(result['packages'],101)
        self.assertEqual(result['remaining'],126.25)

    def test_billing_host_follows_credential_domain(self):
        # 国内站与国际站的计费接口不通用：跨站调用一律 401，必须按凭据域名选站。
        self.assertEqual(billing_host({'X-Domain':'www.workbuddy.ai'}),'https://www.workbuddy.ai')
        self.assertEqual(billing_host({'X-Domain':'WWW.WorkBuddy.AI'}),'https://www.workbuddy.ai')
        self.assertEqual(billing_host({'X-Domain':'www.codebuddy.cn'}),'https://www.codebuddy.cn')
        # 缺失或未知域名回退国内站，保持既有行为。
        self.assertEqual(billing_host({}),'https://www.codebuddy.cn')
        self.assertEqual(billing_host({'X-Domain':'example.com'}),'https://www.codebuddy.cn')

        seen=[]
        def handler(req):
            seen.append(str(req.url))
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{'TotalCount':0,'Accounts':[]}}}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            self.pool.credits(c,{'X-Domain':'www.workbuddy.ai'})
        self.assertTrue(seen[0].startswith('https://www.workbuddy.ai/v2/billing/meter/get-user-resource'),seen[0])
    def test_observe_cost_needs_enough_tokens(self):
        # credit=0 但样本太小：上游对极小请求也会记 0，那不是真免费，不写账本。
        self.pool.observe_cost(self.a,'m',{'credit':0,'total_tokens':150})
        self.assertEqual(self.pool.cost_table(),{})
        self.pool.observe_cost(self.a,'m',{'credit':0,'total_tokens':400})
        self.assertEqual(self.pool.cost_table()['m'],{'cn':'free'})
        # credit>0 与样本大小无关，一律记收费。
        self.pool.observe_cost(self.a,'n',{'credit':0.02,'total_tokens':10})
        self.assertEqual(self.pool.cost_table()['n'],{'cn':'paid'})
        for usage in ({},{'credit':None},{'credit':True},{'credit':'x'}):
            self.pool.observe_cost(self.a,'z',usage)
        self.assertNotIn('z',self.pool.cost_table())
        # 无模型名（auto）不记
        self.pool.observe_cost(self.a,None,{'credit':0,'total_tokens':400})
        self.assertEqual(len(self.pool.cost_table()),2)

    def test_free_site_preference_and_fallback(self):
        doc=credential('intl-one');doc['edition']='intl'
        intl=self.store.save_browser_account(doc,'intl-one')['account_id']
        self.assertEqual(self.pool.site_of(intl),'intl')
        self.assertEqual(self.pool.site_of(self.a),'cn')
        # 只有一边免费：没有可省的钱，不启用优先。
        self.pool.observe_cost(intl,'hy4-preview',{'credit':0,'total_tokens':400})
        self.assertEqual(self.pool.preferred_free_sites('hy4-preview'),set())
        # 国内站实测收费 → 出现「一边免费一边收费」，优先国际站。
        self.pool.observe_cost(self.a,'hy4-preview',{'credit':0.29,'total_tokens':400})
        self.assertEqual(self.pool.preferred_free_sites('hy4-preview'),{'intl'})
        self.assertEqual(self.pool.select(model='hy4-preview')[0],intl)
        # 免费站账号不可用 → 回退到收费站，不报错。
        self.store.data['accounts'][intl]['enabled']=False
        self.assertIn(self.pool.select(model='hy4-preview')[0],(self.a,self.b))
        self.store.data['accounts'][intl]['enabled']=True
        # 都收费 / 都免费 都不启用优先，照常轮询。
        self.pool.observe_cost(intl,'glm-5.3',{'credit':0.79,'total_tokens':400})
        self.pool.observe_cost(self.a,'glm-5.3',{'credit':0.79,'total_tokens':400})
        self.assertEqual(self.pool.preferred_free_sites('glm-5.3'),set())
        self.pool.observe_cost(self.a,'hy3',{'credit':0,'total_tokens':400})
        self.assertEqual(self.pool.preferred_free_sites('hy3'),set())
        # 未知模型不启用优先
        self.assertEqual(self.pool.preferred_free_sites('never-seen'),set())
        self.assertEqual(self.pool.preferred_free_sites(None),set())
    def test_low_balance_stops_paid_models(self):
        # 余额见底后上游连免费模型都整体拒绝，所以留余量、不再接已实测收费的模型。
        self.pool.update(self.a,remaining=30,packages=1,credits_updated=self.now)
        self.pool.update(self.b,remaining=30,packages=1,credits_updated=self.now)
        self.store.data['model_cost']={'paid-model':{'cn':'paid'}}
        with self.assertRaises(HTTPException) as caught:
            self.pool.select(model='paid-model')
        self.assertIn('付费模型',caught.exception.detail)
        self.assertIn('50',caught.exception.detail)
        # 未知模型放行：账本只会慢慢积累，一律拦截会让低余额账号没法用
        self.assertIn(self.pool.select(model='never-seen')[0],(self.a,self.b))
        # 实测免费的模型放行
        self.store.data['model_cost']['free-model']={'cn':'free'}
        self.assertIn(self.pool.select(model='free-model')[0],(self.a,self.b))
        # 余额回升到阈值以上就恢复
        self.pool.update(self.a,remaining=500,credits_updated=self.now)
        self.assertEqual(self.pool.select(model='paid-model')[0],self.a)
        # 阈值调到 0 等于关闭保护
        self.store.data['pool']['min_credits']=0
        self.assertEqual(self.pool.can_serve(self.b,'paid-model'),True)

    def test_low_balance_ignores_unknown_remaining(self):
        # 余额未知（还没查过积分）不拦，否则新导入的账号会完全用不了
        self.store.data['model_cost']={'paid-model':{'cn':'paid'}}
        self.assertEqual(self.pool.remaining_of(self.a),None)
        self.assertEqual(self.pool.can_serve(self.a,'paid-model'),True)


if __name__=='__main__':unittest.main()
