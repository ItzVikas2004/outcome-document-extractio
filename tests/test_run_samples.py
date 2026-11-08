import asyncio
import copy
import json

import httpx
import pytest

from scripts import run_samples as runner

SCHEMA = {'name': 'arbitrary', 'fields': {'custom': {'name': 'Custom', 'description': 'Requested'}}}
RESULT = {'entries': [{'custom': {'value': None, 'citation': '', 'confidence': 0}}],
          'metadata': {'model_calls': 2, 'tokens_in': 10, 'tokens_out': 5, 'cost_usd': 0,
                       'latency_ms': 100, 'cost_source': 'unavailable', 'cost_complete': False}}


def body(status='SUCCESS', result=None, job_id='job'):
    return {'job_id': job_id, 'status': status, 'created_at': '2026-11-08T00:00:00Z',
            'updated_at': '2026-11-08T00:00:01Z', 'result': RESULT if result is None else result}


@pytest.fixture
def args(tmp_path):
    sample = tmp_path / 'external'
    (sample / 'schemas').mkdir(parents=True)
    (sample / 'sample-data-documents').mkdir()
    (sample / 'schemas/arbitrary.json').write_text(json.dumps(SCHEMA))
    (sample / 'manifest.csv').write_text('document_id,file,schema_name,description\none,one.csv,arbitrary,synthetic\ntwo,two.csv,arbitrary,synthetic\n')
    for name in ('one', 'two'):
        (sample / f'sample-data-documents/{name}.csv').write_text('Synthetic,value\n')
    return runner.parser().parse_args(['--sample-dir', str(sample), '--output-dir', str(tmp_path / 'outputs'), '--limit', '1'])


@pytest.mark.parametrize('status', ['SUCCESS', 'PARTIAL'])
def test_multipart_and_result_only_saved(args, status, capsys):
    methods = []
    def respond(request):
        methods.append(request.method)
        if request.method == 'POST':
            content = request.read()
            assert b'name="file"' in content and b'name="schema"' in content
            assert b'Synthetic,value' in content and b'"custom"' in content
            return httpx.Response(202, json=body('PENDING'))
        return httpx.Response(200, json=body(status))
    assert asyncio.run(runner.run(args, httpx.MockTransport(respond))) == 0
    output = json.loads((args.output_dir / 'one.json').read_text())
    assert output == {**RESULT, 'schema': 'arbitrary'}
    assert methods == ['POST', 'GET']
    assert 'unavailable' in capsys.readouterr().out


def test_skip_good_output_limit_counts_new_submissions_and_overwrite(args):
    args.output_dir.mkdir()
    path = args.output_dir / 'one.json'
    path.write_text(json.dumps({**RESULT, 'schema': 'arbitrary'}))
    before = path.stat().st_mtime_ns
    assert runner.prepare(args)[0][0] == 'two'
    assert path.stat().st_mtime_ns == before
    args.overwrite = True
    assert runner.prepare(args)[0][0] == 'one'
    args.overwrite = False
    path.write_text('{}')
    assert runner.prepare(args)[0][0] == 'one'


def test_document_id_and_unknown_selection(args):
    args.document_id = 'two'
    assert [t[0] for t in runner.prepare(args)] == ['two']
    args.document_id = 'unknown'
    with pytest.raises(ValueError):
        runner.prepare(args)


@pytest.mark.parametrize('fault', ['failed', 'missing', 'envelope', 'http', 'nan', 'wrong_id'])
def test_invalid_terminal_does_not_save(args, fault):
    def respond(request):
        if request.method == 'POST':
            return httpx.Response(202, json=body('PENDING'))
        result = copy.deepcopy(RESULT)
        terminal = body()
        if fault == 'failed':
            terminal['status'] = 'FAILED'
        elif fault == 'missing':
            result['entries'][0] = {}
            terminal['result'] = result
        elif fault == 'envelope':
            terminal.pop('result')
        elif fault == 'http':
            return httpx.Response(503)
        elif fault == 'wrong_id':
            terminal['job_id'] = 'different'
        elif fault == 'nan':
            return httpx.Response(200, content=json.dumps(terminal).replace('"cost_usd": 0', '"cost_usd": NaN'))
        return httpx.Response(200, json=terminal)
    assert asyncio.run(runner.run(args, httpx.MockTransport(respond))) == 1
    assert not (args.output_dir / 'one.json').exists()


def test_poll_every_two_seconds_and_timeout(args):
    task = runner.prepare(args)[0]
    now, waits = [0], []
    async def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    async def check():
        async with httpx.AsyncClient(base_url=args.base_url, transport=httpx.MockTransport(
            lambda request: httpx.Response(202 if request.method == 'POST' else 200, json=body('RUNNING')))) as client:
            args.deadline = 5
            return await runner.process(client, task, args, sleep=sleep, clock=lambda: now[0])
    assert not asyncio.run(check())
    assert waits == [2, 2, 1]


def test_concurrency_cap(args):
    args.limit = None
    args.max_in_flight = 1
    active, peak = 0, 0
    async def respond(request):
        nonlocal active, peak
        if request.method == 'POST':
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            return httpx.Response(202, json=body('PENDING'))
        active -= 1
        return httpx.Response(200, json=body())
    assert asyncio.run(runner.run(args, httpx.MockTransport(respond))) == 0
    assert peak == 1 and active == 0


@pytest.mark.parametrize('manifest', ['wrong,columns\nx,y\n',
    'document_id,file,schema_name\none,../outside.pdf,arbitrary\n',
    'document_id,file,schema_name\none,one.csv,arbitrary\none,two.csv,arbitrary\n'])
def test_bad_manifest_rejected_before_network(args, manifest):
    (args.sample_dir / 'manifest.csv').write_text(manifest)
    with pytest.raises(ValueError):
        runner.prepare(args)


def test_cli_failure_exit_and_spending_controls(args):
    assert runner.main(['--sample-dir', str(args.sample_dir / 'missing')]) == 1
    with pytest.raises(SystemExit):
        runner.parser().parse_args(['--sample-dir', '.', '--limit', '0'])


def test_good_selected_output_never_posts_and_failed_overwrite_preserves(args):
    args.document_id = 'one'
    args.output_dir.mkdir()
    path = args.output_dir / 'one.json'
    original = json.dumps({**RESULT, 'schema': 'arbitrary'})
    path.write_text(original)
    def forbidden(request):
        raise AssertionError('Good output must not spend another submission')
    assert asyncio.run(runner.run(args, httpx.MockTransport(forbidden))) == 0
    args.overwrite = True
    assert asyncio.run(runner.run(args, httpx.MockTransport(lambda request: httpx.Response(503)))) == 1
    assert path.read_text() == original
