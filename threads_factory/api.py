from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import hmac
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx

from .generator import Generator
from .models import Batch, Binding, Profile, Schedule, ScheduleChange
from .provider import ProviderError, UploadPost
from .store import Store


@dataclass
class Config:
    api_key: str
    data_dir: str = 'var'
    public_url: str = 'http://127.0.0.1:8000'
    upload_key: str = ''
    upload_profile: str = 'my-threads'
    llm_key: str = ''
    llm_model: str = ''
    llm_base: str = 'https://api.openai.com/v1'
    reference: str = str(Path(__file__).resolve().parent.parent / 'reference')

    @classmethod
    def env(cls):
        load_dotenv(interpolate=False)
        return cls(os.getenv('FACTORY_API_KEY', ''), os.getenv('DATA_DIR','var'),
            os.getenv('PUBLIC_BASE_URL','http://127.0.0.1:8000'), os.getenv('UPLOAD_POST_API_KEY',''),
            os.getenv('UPLOAD_POST_PROFILE','my-threads'), os.getenv('OPENAI_API_KEY',''),
            os.getenv('OPENAI_MODEL',''), os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1'),
            os.getenv('REFERENCE_DIR', str(Path(__file__).resolve().parent.parent / 'reference')))


def create_app(config=None, *, provider=None, generator=None):
    cfg = config or Config.env()
    if len(cfg.api_key) < 32:
        raise ValueError('FACTORY_API_KEY должен содержать минимум 32 символа. Выполните threads-factory init.')
    store = Store(cfg.data_dir)
    pub = provider or UploadPost(cfg.upload_key, cfg.upload_profile)
    gen = generator or Generator(cfg.llm_key, cfg.llm_model, cfg.reference, cfg.llm_base)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if provider is None:
            pub.close()
        if generator is None:
            gen.close()

    app = FastAPI(title='Threads контент завод', version='0.1.0', lifespan=lifespan,
        description='Один владелец на экземпляр. Интервью → профиль → аккаунт → расписание → публикации.',
        servers=[{'url': cfg.public_url}])
    app.state.store = store
    bearer = HTTPBearer(auto_error=False)

    def authorized(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not hmac.compare_digest(credentials.credentials, cfg.api_key):
            raise HTTPException(401, 'Нужен ключ этого экземпляра контент-завода', headers={'WWW-Authenticate':'Bearer'})

    auth = [Depends(authorized)]

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': exc.code}, status_code=502)

    @app.exception_handler(httpx.HTTPError)
    async def network_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'provider_unavailable_check_status_before_retry'}, status_code=502)

    @app.get('/health', include_in_schema=False)
    def health():
        return {'ok': True}

    @app.get('/status', dependencies=auth, operation_id='getFactoryStatus')
    def status():
        """Прочитать профиль, частоту, подключённый аккаунт и очередь. Без секретов."""
        posts = [{**j, 'text': j['text'][:800], 'text_truncated': len(j['text']) > 800} for j in store.jobs(30)]
        return {**store.config(), 'generator_configured': gen.configured,
                'publishing_configured': bool(cfg.upload_key), 'posts': posts}

    @app.put('/profile', dependencies=auth, operation_id='saveNicheProfile')
    def profile(body: Profile):
        """Сохранить ответы интервью. Неотправленные слоты отменяются, тексты остаются в истории."""
        return store.update('profile', body.model_dump())

    @app.post('/connection/start', dependencies=auth, operation_id='startThreadsConnection')
    def connection_start():
        """Получить одноразовую ссылку подключения Threads. Человек входит на стороне провайдера."""
        return pub.connect_url()

    @app.get('/connection', dependencies=auth, operation_id='checkThreadsConnection')
    def connection():
        """Показать действительное имя и идентификатор подключённого аккаунта."""
        return pub.connection()

    @app.post('/connection/bind', dependencies=auth, operation_id='confirmThreadsAccount', openapi_extra={'x-openai-isConsequential': True})
    def bind(body: Binding):
        """Привязать только показанный и подтверждённый человеком аккаунт."""
        account = pub.connection()
        if not body.confirmed or not account.get('connected') or account['account_id'] != body.account_id:
            raise HTTPException(409, 'Сначала проверьте имя и подтвердите точный аккаунт')
        return store.update('account', account)

    @app.put('/schedule', dependencies=auth, operation_id='configurePublishingSchedule', openapi_extra={'x-openai-isConsequential': True})
    def schedule(body: ScheduleChange):
        """Из чата задать постов в день, часовой пояс и время. Автогенерация — отдельное явное согласие."""
        current = store.config()
        if not body.confirmed:
            raise HTTPException(400, 'Нужно подтверждение расписания человеком')
        if body.enabled and (not current['profile'] or not current['account']):
            raise HTTPException(409, 'Сначала интервью и подтверждение подключённого аккаунта')
        if body.enabled and body.autonomous_generation and not gen.configured:
            raise HTTPException(409, 'Для фоновой генерации задайте OPENAI_API_KEY и OPENAI_MODEL на сервере')
        value = Schedule.model_validate(body.model_dump(exclude={'confirmed'}))
        result = store.update('schedule', value.model_dump())
        return {**result, 'next_slots': [s.isoformat() for s in value.slots(datetime.now(UTC)) if s > datetime.now(UTC)][:value.posts_per_day],
                'note': 'В процессе отправки уже начатый запрос может завершиться после паузы.'}

    @app.post('/pause', dependencies=auth, operation_id='pausePublishing')
    def pause():
        """Остановить новые отправки и отменить ещё не отправленные слоты. Тексты сохраняются."""
        value = store.config()['schedule']
        value['enabled'] = False
        return store.update('schedule', value)

    @app.post('/queue', dependencies=auth, operation_id='queueApprovedPosts', openapi_extra={'x-openai-isConsequential': True})
    def queue(body: Batch):
        """Поставить утверждённые тексты в ближайшие свободные слоты. request_id защищает от дубля пакета."""
        try:
            return store.enqueue(body, datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/references', dependencies=auth, operation_id='getReferenceExamples')
    def references(offset: int = Query(0, ge=0, le=63), limit: int = Query(3, ge=1, le=5)):
        """Взять авторские объяснения механик из базы для оригинальной адаптации к нише."""
        return {'total': len(gen.references), 'items': gen.references[offset:offset+limit]}

    return app
