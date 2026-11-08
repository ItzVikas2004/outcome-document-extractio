"""Explicitly invoked assignment API client; never reads reference answers."""
import argparse
import asyncio
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = Draft202012Validator(json.loads((ROOT / 'contract/output.schema.json').read_text()))
STATES = {'PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED'}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('Invalid manifest document_id or schema_name')
    return value


def contained(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Manifest path escapes sample directory')
    return path


def valid_result(result, fields):
    if not VALIDATOR.is_valid(result):
        raise ValueError('Invalid extraction result contract')
    json.dumps(result, allow_nan=False)
    for entry in result['entries']:
        if set(entry) != set(fields):
            raise ValueError('Result must include exactly every requested field')
        if any(f['value'] is None and f['citation'] != '' for f in entry.values()):
            raise ValueError('Null field has a nonempty citation')


def good_output(path, schema_name, fields):
    try:
        body = read_json(path)
        if set(body) != {'schema', 'entries', 'metadata'} or body['schema'] != schema_name:
            return False
        valid_result({k: body[k] for k in ('entries', 'metadata')}, fields)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def prepare(args):
    root = args.sample_dir.resolve()
    with (root / 'manifest.csv').open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'document_id', 'file', 'schema_name'} <= set(reader.fieldnames or []):
            raise ValueError('manifest.csv requires document_id,file,schema_name columns')
        rows = list(reader)
    ids = [identifier(row.get('document_id')) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate document_id in manifest')
    if args.document_id and args.document_id not in ids:
        raise ValueError('Requested document_id is not in manifest')
    tasks = []
    for row in rows:
        if args.document_id and row['document_id'] != args.document_id:
            continue
        name = identifier(row.get('schema_name'))
        schema = read_json(contained(root, f'schemas/{name}.json'))
        if (not isinstance(schema, dict) or not isinstance(schema.get('name'), str)
                or not isinstance(schema.get('fields'), dict) or not schema['fields']):
            raise ValueError('Invalid field-list schema')
        for field in schema['fields'].values():
            if not isinstance(field, dict) or not all(isinstance(field.get(k), str) for k in ('name', 'description')):
                raise ValueError('Invalid field-list field definition')
        output = args.output_dir / (row['document_id'] + '.json')
        if not args.overwrite and good_output(output, name, schema['fields']):
            print(f"{row['document_id']} SKIPPED valid existing output", flush=True)
            continue
        relative = row.get('file')
        if not relative:
            raise ValueError('Missing manifest file')
        document = contained(root, relative)
        if not document.is_file():
            document = contained(root, 'sample-data-documents/' + relative)
        if not document.is_file() or document.suffix.lower() not in {'.pdf', '.docx', '.xlsx', '.csv'}:
            raise ValueError(f"Missing or unsupported document: {row['document_id']}")
        if document.stat().st_size > 50 * 1024 * 1024:
            raise ValueError('Document exceeds 50 MB')
        tasks.append((row['document_id'], name, schema, document, output))
        if args.limit is not None and len(tasks) >= args.limit:
            break
    return tasks


def envelope(response, expected_status, job_id=None):
    if response.status_code != expected_status:
        raise ValueError(f'API HTTP {response.status_code}')
    body = response.json()
    if (not isinstance(body, dict) or body.get('status') not in STATES
            or not isinstance(body.get('job_id'), str) or not body['job_id']
            or (job_id is not None and body['job_id'] != job_id)):
        raise ValueError('Invalid job response')
    for key in ('created_at', 'updated_at'):
        if not isinstance(body.get(key), str) or datetime.fromisoformat(body[key].replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('Invalid job timestamp')
    return body


def save(output, body):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(body, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def process(client, task, args, sleep=asyncio.sleep, clock=time.monotonic):
    document_id, name, schema, document, output = task
    start = clock()
    try:
        # Includes upload and polling; waiting for our concurrency slot is not a submitted job.
        async with asyncio.timeout(args.deadline):
            with document.open('rb') as handle:
                response = await client.post('/jobs', files={'file': (document.name, handle)},
                                             data={'schema': json.dumps(schema, allow_nan=False)})
            body = envelope(response, 202)
            job_id = body['job_id']
            print(f'{document_id} ACCEPTED job_id={job_id} submission_s={clock() - start:.3f}', flush=True)
            while clock() - start < args.deadline:
                body = envelope(await client.get('/jobs/' + quote(job_id, safe='')), 200, job_id)
                status = body['status']
                if status == 'FAILED':
                    print(f'{document_id} FAILED job_id={job_id}', flush=True)
                    raise ValueError('Job FAILED')
                if status in {'SUCCESS', 'PARTIAL'}:
                    result = body.get('result')
                    valid_result(result, schema['fields'])
                    if args.overwrite or not good_output(output, name, schema['fields']):
                        save(output, {**result, 'schema': name})
                    usage = {k: result['metadata'].get(k) for k in (
                        'model_calls', 'tokens_in', 'tokens_out', 'cost_usd', 'cost_source',
                        'cost_complete', 'usage_complete', 'cost_is_known_subtotal', 'latency_ms')}
                    print(f'{document_id} {status} elapsed_s={clock() - start:.3f} usage={json.dumps(usage)}', flush=True)
                    return True
                await sleep(min(2, max(0, args.deadline - (clock() - start))))
            raise TimeoutError()
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError, TimeoutError) as exc:
        # Never print HTTP bodies, documents, URLs with credentials or exception payloads.
        label = 'TIMEOUT' if isinstance(exc, TimeoutError) else 'ERROR'
        print(f'{document_id} {label} elapsed_s={clock() - start:.3f} reason={type(exc).__name__}', flush=True)
        return False


async def run(args, transport=None):
    tasks = prepare(args)  # Preflight entire selected batch before any submissions.
    semaphore = asyncio.Semaphore(args.max_in_flight)
    async with httpx.AsyncClient(base_url=args.base_url.rstrip('/'), transport=transport,
                                 timeout=30, follow_redirects=False, trust_env=False) as client:
        async def limited(task):
            async with semaphore:
                return await process(client, task, args)
        outcomes = await asyncio.gather(*(limited(task) for task in tasks))
    print(f'Submitted={len(outcomes)} completed={sum(outcomes)} failed={len(outcomes) - sum(outcomes)}', flush=True)
    return 0 if all(outcomes) else 1


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--sample-dir', type=Path, required=True)
    cli.add_argument('--base-url', default='http://localhost:8080')
    cli.add_argument('--limit', type=positive)
    cli.add_argument('--max-in-flight', type=positive, default=1)
    cli.add_argument('--document-id')
    cli.add_argument('--overwrite', action='store_true')
    cli.add_argument('--output-dir', type=Path, default=Path('outputs'))
    cli.add_argument('--deadline', type=positive, default=1800)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    if args.deadline > 1800:
        parser().error('--deadline cannot exceed the assignment limit of 1800 seconds')
    try:
        return asyncio.run(run(args))
    except (OSError, ValueError, TypeError) as exc:
        print(f'Input/configuration error: {type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
