from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from threads_factory.api import Config, create_app
from threads_factory.engine import Engine
from threads_factory.generator import Generator
from threads_factory.models import Batch, Profile, Schedule
from threads_factory.provider import ProviderError, UploadPost, receipt
from threads_factory.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
URL = 'https://www.threads.com/@demo/post/one'
KEY = 'test-key-' + 'x' * 50
PROFILE = {'name': 'Анна', 'niche': 'Дизайн интерьеров', 'audience': 'Владельцы квартир',
           'topics': ['Освещение', 'Планировка'], 'facts': ['Работаю с маленькими квартирами']}
REFERENCE = Path(__file__).resolve().parents[1] / 'reference'


class Publisher:
    def __init__(self):
        self.calls, self.polls = 0, 0
        self.identity = 'opaque-id'
        self.timeout = False
        self.status_result = {'status': 'not_found'}

    def connection(self):
        return {'connected': True, 'account_id': self.identity, 'handle': '@demo'}

    def connect_url(self):
        return {'url': 'https://app.upload-post.com/connect?token=synthetic'}

    def publish(self, identity, text):
        self.calls += 1
        if self.timeout:
            raise httpx.ReadTimeout('synthetic timeout')
        return {'results': {'threads': {'success': True, 'url': URL}}}

    def status(self, identity):
        self.polls += 1
        return self.status_result


class Writer:
    configured = True
    references = []

    def __init__(self):
        self.calls = 0
        self.callback = None
        self.timeout = False

    def generate(self, profile, history, slot):
        self.calls += 1
        if self.callback:
            self.callback()
        if self.timeout:
            raise httpx.ReadTimeout('synthetic generation timeout')
        return 'Свет в маленькой квартире стоит планировать по задачам. Для чтения нужен отдельный источник, для спокойного вечера — мягкий общий свет.', 'adaptation-001'


@pytest.fixture
def factory(tmp_path):
    store = Store(tmp_path)
    store.update('profile', Profile.model_validate(PROFILE).model_dump())
    store.update('account', {'account_id': 'opaque-id', 'handle': '@demo'})
    store.update('schedule', Schedule(enabled=True, posts_per_day=1, timezone='UTC', times=['12:00'], autonomous_generation=True).model_dump())
    return store, Publisher(), Writer()


def test_full_cycle_and_repeat_tick_publish_once(factory):
    store, pub, writer = factory
    engine = Engine(store, pub, writer)
    engine.tick(NOW)
    engine.tick(NOW + timedelta(minutes=1))
    assert pub.calls == writer.calls == 1
    assert store.jobs()[0]['state'] == 'published'
    assert store.jobs()[0]['url'] == URL


def test_prepare_early_but_do_not_send_early(factory):
    store, pub, writer = factory
    Engine(store, pub, writer).tick(NOW - timedelta(minutes=30))
    assert store.jobs()[0]['state'] == 'ready'
    assert writer.calls == 1 and pub.calls == 0


def test_unknown_publish_never_reposts_and_reconciles_receipt(factory):
    store, pub, writer = factory
    pub.timeout = True
    engine = Engine(store, pub, writer)
    engine.tick(NOW)
    assert store.jobs()[0]['state'] == 'unknown'
    engine.tick(NOW + timedelta(minutes=1))
    assert pub.calls == 1
    pub.status_result = {'results': [{'platform': 'threads', 'success': True, 'post_url': URL}]}
    engine.tick(NOW + timedelta(minutes=2))
    assert store.jobs()[0]['state'] == 'published' and pub.calls == 1


def test_generation_timeout_is_not_a_second_paid_call(factory):
    store, pub, writer = factory
    writer.timeout = True
    engine = Engine(store, pub, writer)
    engine.tick(NOW)
    engine.tick(NOW + timedelta(minutes=1))
    assert writer.calls == 1 and pub.calls == 0
    assert store.jobs()[0]['state'] == 'generation_unknown'


def test_worker_recovery_preserves_unknown_without_dispatch(factory):
    store, pub, writer = factory
    job = store.ensure(NOW.isoformat(), store.config()['revision'], 'Утверждённый человеком текст поста')
    assert store.claim(job['id'], 'ready', 'sending')
    store.recover()
    Engine(store, pub, writer).tick(NOW)
    assert pub.calls == writer.calls == 0
    assert store.jobs()[0]['state'] == 'unknown'


def test_account_change_blocks_wrong_destination(factory):
    store, pub, writer = factory
    pub.identity = 'different-account'
    Engine(store, pub, writer).tick(NOW)
    assert pub.calls == 0
    assert store.jobs()[0]['state'] == 'blocked'


def test_pause_or_profile_change_during_generation_fences_send(factory):
    store, pub, writer = factory
    writer.callback = lambda: store.update('profile', {**PROFILE, 'name': 'Новый профиль'})
    Engine(store, pub, writer).tick(NOW)
    assert pub.calls == 0 and store.jobs()[0]['state'] == 'cancelled'


def test_pause_preserves_text_and_prevents_send(factory):
    store, pub, writer = factory
    Engine(store, pub, writer).tick(NOW - timedelta(minutes=10))
    cfg = store.config()['schedule']; cfg['enabled'] = False
    store.update('schedule', cfg)
    Engine(store, pub, writer).tick(NOW)
    assert store.jobs()[0]['text'] and store.jobs()[0]['state'] == 'cancelled'
    assert pub.calls == 0


def test_atomic_claim_is_available_once(factory):
    store, _, _ = factory
    job = store.ensure(NOW.isoformat(), store.config()['revision'], 'Текст, который может отправить только один worker')
    assert store.claim(job['id'], 'ready', 'sending')
    assert Store(store.path.parent).claim(job['id'], 'ready', 'sending') is None


def test_batch_idempotence_and_preserved_schedule(factory):
    store, _, _ = factory
    body = Batch(request_id='approved-batch-1', posts=['Первый утверждённый текст публикации', 'Второй утверждённый текст публикации'], approved=True)
    first = store.enqueue(body, NOW - timedelta(hours=1))
    assert store.enqueue(body, NOW - timedelta(hours=1)) == first
    assert len(store.jobs()) == 2 and first['slots'][0] == NOW.isoformat()
    with pytest.raises(ValueError):
        store.enqueue(body.model_copy(update={'posts': ['Другой текст с тем же идентификатором']}), NOW)


def test_unapproved_batch_cannot_publish(factory):
    store, _, _ = factory
    with pytest.raises(ValueError):
        store.enqueue(Batch(request_id='unapproved-1', posts=['Пост ещё не утверждён пользователем'], approved=False), NOW)
    assert not store.jobs()


@pytest.mark.parametrize('zone,date,time,expected', [
    ('America/New_York', '2026-03-08T00:00:00+00:00', '02:30', 0),
    ('America/New_York', '2026-11-01T00:00:00+00:00', '01:30', 1),
])
def test_dst_never_manufactures_or_duplicates_slot(zone, date, time, expected):
    now = datetime.fromisoformat(date)
    slots = Schedule(posts_per_day=1, timezone=zone, times=[time]).slots(now, days=2)
    target = datetime.fromisoformat(date).date()
    from zoneinfo import ZoneInfo
    assert sum(s.astimezone(ZoneInfo(zone)).date() == target for s in slots) == expected


def test_frequency_is_exact_and_clock_is_local():
    schedule = Schedule(posts_per_day=7, timezone='Europe/Moscow')
    now = datetime(2026, 10, 29, 0, tzinfo=UTC)
    assert len(schedule.slots(now, days=1)) == 7
    assert len(set(schedule.slots(now, days=1))) == 7


def test_api_auth_consent_and_no_silent_autopilot(tmp_path):
    app = create_app(Config(KEY, data_dir=str(tmp_path)), provider=Publisher(), generator=Writer())
    with TestClient(app) as client:
        assert client.get('/status').status_code == 401
        assert client.put('/profile', json=PROFILE).status_code == 401
        headers = {'Authorization': 'Bearer ' + KEY}
        assert client.get('/status', headers=headers).json()['schedule']['enabled'] is False
        assert client.put('/profile', headers=headers, json=PROFILE).status_code == 200
        assert client.post('/connection/bind', headers=headers, json={'confirmed': False,'account_id':'opaque-id'}).status_code == 409
        assert client.post('/connection/bind', headers=headers, json={'confirmed': True,'account_id':'opaque-id'}).status_code == 200
        assert client.put('/schedule', headers=headers, json={'enabled': True}).status_code == 400
        assert client.put('/schedule', headers=headers, json={'enabled': True,'confirmed': True,'posts_per_day':4}).status_code == 200
        state = client.get('/status', headers=headers).json()
        assert state['schedule']['posts_per_day'] == 4 and not state['schedule']['autonomous_generation']
        assert KEY not in str(state)
        assert client.put('/schedule', headers=headers, json={'timezone':'Invalid/Zone','confirmed':True}).status_code == 422
        assert client.post('/pause', headers=headers).json()['schedule']['enabled'] is False
    schema = app.openapi()
    assert schema['paths']['/schedule']['put']['x-openai-isConsequential']
    assert schema['paths']['/queue']['post']['security']


def test_provider_uses_stable_identity_and_request_ids():
    calls = []
    def transport(request):
        calls.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'profiles':[{'username':'my-profile','social_accounts':{'threads':{'username':'opaque-id','handle':'@human'}}}]})
        return httpx.Response(200, json={'success':True,'request_id':'post-id'})
    provider = UploadPost('synthetic-key','my-profile',transport=httpx.MockTransport(transport))
    assert provider.connection()['account_id'] == 'opaque-id'
    assert provider.connection()['handle'] == '@human'
    provider.publish('stable-post-id','Утверждённый текст')
    form = parse_qs(calls[-1].content.decode())
    assert form['platform[]'] == ['threads'] and form['request_id'] == ['stable-post-id']
    assert calls[-1].headers['Idempotency-Key'] == 'stable-post-id'
    assert form['async_upload'] == ['true']
    provider.close()


@pytest.mark.parametrize('payload', [
    {'success': True, 'status':'completed'},
    {'results':{'threads':{'success':True,'url':'https://evil.example/post/stolen'}}},
    {'results':{'threads':{'success':True,'url':'https://user:secret@threads.com/@demo/post/id'}}},
    {'results':{'threads':{'success':True,'url':None}}},
])
def test_no_published_label_without_threads_receipt(payload):
    assert receipt(payload)[0] == 'unknown'


@pytest.mark.parametrize('payload', [None, [], {'results': None}, {'results': [None, 'bad']},
    {'results': {'threads': None}}, {'results': {'threads': {'success': True, 'url': 42}}}])
def test_malformed_provider_reply_remains_unknown(factory, payload):
    store, pub, writer = factory
    pub.publish = lambda identity, text: payload
    engine = Engine(store, pub, writer)
    engine.tick(NOW)
    assert store.jobs()[0]['state'] == 'unknown'
    pub.status_result = payload
    engine.tick(NOW + timedelta(minutes=1))
    assert store.jobs()[0]['state'] == 'unknown'


@pytest.mark.parametrize('payload', [None, {'status': 'completed', 'output': None},
    {'status': 'completed', 'output': [{'type': 'message', 'content': None}]},
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '[]'}]}]}])
def test_malformed_generation_finishes_instead_of_stalling(factory, payload):
    store, pub, _ = factory
    gen = Generator('synthetic', 'chosen-model', REFERENCE,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    engine = Engine(store, pub, gen)
    try:
        engine.tick(NOW)
        assert store.jobs()[0]['state'] == 'generation_failed' and pub.calls == 0
    finally:
        gen.close()


def test_ai_real_wire_contract_and_source_copy_guard():
    calls = []
    def respond(request):
        payload = json.loads(request.content); calls.append(payload)
        context = json.loads(payload['input'])
        ref = context['references'][0]['id']
        text = 'Маленькая кухня становится удобнее, когда у каждой зоны есть своя задача. Начните с того, где вы готовите чаще всего, и подстройте хранение под этот маршрут.'
        if len(calls) == 2:
            text = ' '.join(json.loads((REFERENCE/'data/ocr.jsonl').read_text().splitlines()[0])['text'].split()[:30])
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'text':text,'reference_id':ref})}]}]})
    gen = Generator('synthetic','chosen-model',REFERENCE,transport=httpx.MockTransport(respond))
    result = gen.generate(PROFILE,[],NOW.isoformat())
    assert result[0].startswith('Маленькая кухня')
    assert calls[0]['model'] == 'chosen-model' and calls[0]['store'] is False
    with pytest.raises(ValueError, match='source_copy_rejected'):
        gen.generate(PROFILE,[],NOW.isoformat())
    assert len(calls) == 2
    gen.close()


def test_complete_reference_bundle():
    photos = list((REFERENCE/'photos').glob('*.jpg'))
    assert len([p for p in photos if '_thumb' not in p.name]) == 351
    adaptations = json.loads((REFERENCE/'data/adaptations.json').read_text())
    assert len(adaptations) == 64
    assert sum(len(a['numberedItems']) for a in adaptations) == 318
    assert len((REFERENCE/'data/ocr.jsonl').read_text().splitlines()) == 351
