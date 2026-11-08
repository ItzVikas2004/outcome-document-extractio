"""Network/API tests with database audits. Run only in compose.load.yaml."""
import argparse
import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Job
from tests.mock_provider import TABLE_SCHEMA

TERMINAL = {'SUCCESS', 'PARTIAL', 'FAILED'}
VALIDATOR = Draft202012Validator(json.loads(Path('contract/output.schema.json').read_text()))
ENGINE = create_engine(os.environ['DATABASE_URL'])


def audit(identifier):
    with Session(ENGINE) as session:
        job = session.get(Job, identifier)
        return {'attempts': job.attempt_count, 'calls': job.llm_usage,
                'errors': job.error_history, 'status': job.status,
                'finished_at': job.finished_at.isoformat() if job.finished_at else None}


def percentiles(values):
    # Nearest-rank: p95 of ten samples is the maximum; no interpolation fiction.
    ordered = sorted(values)
    return {f'p{p}': ordered[math.ceil(p / 100 * len(ordered)) - 1] for p in (50, 95)} if ordered else {}


def check_output(body):
    assert body['status'] == 'SUCCESS', body.get('error')
    result = body['result']
    VALIDATOR.validate(result)
    assert len(result['entries']) == 2
    for entry in result['entries']:
        assert set(entry) == set(TABLE_SCHEMA['fields'])
    absent = result['entries'][1]['date_custom']
    assert absent['value'] is None and absent['citation'] == ''
    assert result['entries'][0]['amount_custom']['value'] == 1200.5


async def submit(client, tenant, token, variant=False):
    schema = json.dumps(TABLE_SCHEMA, sort_keys=variant, indent=2 if variant else None)
    content = f'Property: Offline House,,\nUnit,Rent,Date\n{token}-A,1200.50,2026-01-02\n{token}-B,900,\n'
    start = time.perf_counter()
    response = await client.post('/jobs', headers={'X-Organization-Id': tenant},
                                 data={'schema': schema}, files={'file': ('synthetic.csv', content.encode(), 'text/csv')})
    elapsed = time.perf_counter() - start
    assert response.status_code == 202, (response.status_code, response.text)
    body = response.json()
    assert {'job_id', 'status', 'created_at', 'updated_at'} <= body.keys()
    return {'job_id': body['job_id'], 'tenant': tenant, 'start': start,
            'submission_seconds': elapsed, 'under_two_seconds': elapsed < 2}


async def poll(client, record, timeout):
    while time.perf_counter() - record['start'] < timeout:
        response = await client.get('/jobs/' + record['job_id'], headers={'X-Organization-Id': record['tenant']})
        assert response.status_code == 200
        body = response.json()
        if body['status'] in TERMINAL:
            record['completion_seconds'] = time.perf_counter() - record['start']
            record['terminal'] = body['status']
            check_output(body)
            before = audit(record['job_id'])
            await asyncio.sleep(2)
            again = await client.get('/jobs/' + record['job_id'], headers={'X-Organization-Id': record['tenant']})
            assert again.json() == body, 'terminal response changed'
            assert audit(record['job_id']) == before, 'processing continued after terminal outcome'
            record['audit'] = before
            return record
        await asyncio.sleep(2)
    raise AssertionError('job timed out: ' + record['job_id'])


async def run(args, report):
    tenant = 'load-' + uuid4().hex
    async with httpx.AsyncClient(base_url='http://api:8080', timeout=10, trust_env=False) as client:
        for _ in range(60):
            try:
                if (await client.get('/health')).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:
            raise AssertionError('API not healthy')
        if args.recovery:
            records = [await submit(client, tenant, 'crash')]
            # Wait for an actual dispatched provider request before asking host to kill.
            for _ in range(200):
                if audit(records[0]['job_id'])['calls']:
                    Path('/reports/kill-ready').write_text(records[0]['job_id'])
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError('worker did not begin model request')
        else:
            records = await asyncio.gather(*(submit(client, tenant, str(i)) for i in range(10)))
            assert len({r['job_id'] for r in records}) == 10
        report['jobs'] = records
        outcomes = await asyncio.gather(*(poll(client, r, args.timeout) for r in records), return_exceptions=True)
        errors = [str(item) for item in outcomes if isinstance(item, BaseException)]
        report['submission_seconds'] = percentiles([r['submission_seconds'] for r in records])
        report['completion_seconds'] = percentiles([r['completion_seconds'] for r in records if 'completion_seconds' in r])
        report['post_under_two_seconds'] = all(r['under_two_seconds'] for r in records)
        completed = [r for r in records if r.get('terminal') == 'SUCCESS']
        report['outcomes'] = {state: sum(r.get('terminal') == state for r in records) for state in TERMINAL}
        report['without_terminal_observation'] = sum('terminal' not in r for r in records)
        if completed:
            duration = max(r['start'] + r['completion_seconds'] for r in completed) - min(r['start'] for r in records)
            report['observed_batch_seconds'] = duration
            report['successful_jobs_per_second'] = len(completed) / duration
        assert not errors, errors
        assert report['post_under_two_seconds'], 'POST exceeded two seconds'
        if args.recovery:
            evidence = records[0]['audit']
            assert evidence['attempts'] == 2
            assert any(e['code'] == 'WORKER_LOST' for e in evidence['errors'])
            assert {c['attempt'] for c in evidence['calls']} == {1, 2}
            report['recovery_passed'] = True
        else:
            assert all(r['audit']['attempts'] == 1 and len(r['audit']['calls']) == 2 for r in records)
            assert all(not r['audit']['errors'] and all(c['state'] == 'completed' for c in r['audit']['calls']) for r in records)
            duplicates = await asyncio.gather(*(submit(client, tenant, 'duplicate', bool(i % 2)) for i in range(10)))
            assert all(r['under_two_seconds'] for r in duplicates)
            assert len({r['job_id'] for r in duplicates}) == 1
            duplicate = await poll(client, duplicates[0], args.timeout)
            assert duplicate['audit']['attempts'] == 1 and len(duplicate['audit']['calls']) == 2
            other = await submit(client, tenant + '-other', 'duplicate')
            assert other['job_id'] != duplicate['job_id']
            for identifier, wrong in [(other['job_id'], tenant), (duplicate['job_id'], tenant + '-other')]:
                assert (await client.get('/jobs/' + identifier, headers={'X-Organization-Id': wrong})).status_code == 404
                assert (await client.get('/jobs/' + identifier)).status_code == 404
            other = await poll(client, other, args.timeout)
            assert other['audit']['attempts'] == 1 and len(other['audit']['calls']) == 2
            report['deduplication_and_isolation_passed'] = True
            report['duplicate_submissions'] = duplicates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True)
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--recovery', action='store_true')
    args = parser.parse_args()
    report = {'label': args.label, 'passed': False, 'poll_interval_seconds': 2,
              'started_at': datetime.now(timezone.utc).isoformat(),
              'percentile_method': 'nearest-rank', 'provider_delay_seconds_per_call': 1}
    try:
        asyncio.run(run(args, report))
        report['passed'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        ENGINE.dispose()
        Path('/reports', args.label + '.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k not in {'jobs', 'duplicate_submissions'}}, indent=2), flush=True)
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
