import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import time

from .api import Config, create_app
from .engine import Engine
from .generator import Generator
from .provider import UploadPost
from .store import Store


def initialize():
    path = Path('.env')
    if path.exists():
        raise SystemExit('.env уже существует; он не перезаписан.')
    print('Ключи вводятся локально. Не отправляйте их в чат или в GitHub.')
    upload_key = getpass.getpass('API key Upload-Post: ').strip()
    profile = input('Профиль Upload-Post [my-threads]: ').strip() or 'my-threads'
    model = input('Модель OpenAI Responses API для фоновой генерации (Enter — только тексты из чата): ').strip()
    llm_key = getpass.getpass('OpenAI API key: ').strip() if model else ''
    public = input('HTTPS-адрес вашего сервиса [http://127.0.0.1:8000]: ').strip() or 'http://127.0.0.1:8000'
    values = {'FACTORY_API_KEY': secrets.token_urlsafe(48), 'PUBLIC_BASE_URL': public, 'DATA_DIR': 'var',
        'UPLOAD_POST_API_KEY': upload_key, 'UPLOAD_POST_PROFILE': profile, 'OPENAI_API_KEY': llm_key,
        'OPENAI_MODEL': model, 'OPENAI_BASE_URL': 'https://api.openai.com/v1'}
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as f:
        f.write('\n'.join(k + '=' + json.dumps(v, ensure_ascii=False) for k, v in values.items()) + '\n')
    print('Готово. .env сохранён с правами 600. FACTORY_API_KEY понадобится в настройке личного GPT/Claude, не в сообщении чата.')


def worker(once=False):
    try:
        import fcntl
    except ImportError:
        raise SystemExit('Для worker в Windows используйте WSL2 или Docker Compose.') from None
    cfg = Config.env()
    store = Store(cfg.data_dir)
    with open(Path(cfg.data_dir) / 'worker.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Worker уже работает с этой базой.')
        provider = UploadPost(cfg.upload_key, cfg.upload_profile)
        generator = Generator(cfg.llm_key, cfg.llm_model, cfg.reference, cfg.llm_base)
        store.recover()
        engine = Engine(store, provider, generator)
        try:
            while True:
                try:
                    engine.tick()
                except Exception as exc:
                    # Never print provider payloads, keys, connect URLs or private profile content.
                    print('Worker tick failed: ' + type(exc).__name__, flush=True)
                    if once:
                        raise
                if once:
                    return
                time.sleep(60)
        finally:
            provider.close()
            generator.close()


def main():
    parser = argparse.ArgumentParser(description='Threads контент завод')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8000)
    work = sub.add_parser('worker')
    work.add_argument('--once', action='store_true')
    schema = sub.add_parser('schema')
    schema.add_argument('--url', default='https://threads.example.com')
    sub.add_parser('mcp')
    args = parser.parse_args()
    if args.command == 'init':
        initialize()
    elif args.command == 'serve':
        import uvicorn
        uvicorn.run('threads_factory.api:create_app', factory=True, host=args.host, port=args.port)
    elif args.command == 'worker':
        worker(args.once)
    elif args.command == 'schema':
        app = create_app(Config(api_key='schema-export-placeholder-not-a-real-key', public_url=args.url))
        print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
    else:
        from .mcp_server import main as serve_mcp
        serve_mcp()


if __name__ == '__main__':
    main()
