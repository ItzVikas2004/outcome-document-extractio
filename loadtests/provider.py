"""Deterministic synthetic CSV provider, isolated on an internal Docker network."""
import asyncio
import json

from fastapi import FastAPI, Request
from tests.mock_provider import TABLE_PLAN, table_response

app = FastAPI()


@app.post('/v1/chat/completions')
async def completions(request: Request):
    body = await request.json()
    payload = json.loads(body['messages'][1]['content'])
    # Fixed service time makes worker scaling observable; no provider randomness.
    await asyncio.sleep(1)
    result = TABLE_PLAN if payload['task'] == 'plan' else table_response(payload)
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'cost': 0}}
