import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import uvicorn

from threads_factory.api import Config, create_app


def test_real_mcp_stdio_to_authenticated_local_api(tmp_path):
    key = 'synthetic-mcp-test-' + 'x' * 40
    app = create_app(Config(key, data_dir=str(tmp_path)))
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started

        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                args=['-m','threads_factory.cli','mcp'],
                env={**os.environ, 'FACTORY_API_KEY':key, 'FACTORY_BASE_URL':base})
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 9
                names = {t.name for t in tools.tools}
                assert {'get_factory_status','configure_publishing_schedule','queue_approved_posts'} <= names
                result = await session.call_tool('get_factory_status', {})
                assert not result.isError
                data = json.loads(next(c.text for c in result.content if c.type == 'text'))
                assert data['schedule']['enabled'] is False and data['profile'] is None
                resource = await session.read_resource('factory://instructions')
                assert 'Сначала интервью' in resource.contents[0].text
        asyncio.run(exercise())
        assert httpx.get(base + '/status').status_code == 401
    finally:
        server.should_exit = True
        thread.join(5)
        assert not thread.is_alive()
