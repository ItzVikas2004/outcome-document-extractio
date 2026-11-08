"""Host-side, stdlib-only orchestration. Controls only the fixed offline project."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker', default='docker')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    reports = root / 'load_reports'
    reports.mkdir(exist_ok=True)
    # Runner uses the unprivileged application UID; reports contain synthetic data only.
    reports.chmod(0o777)
    (reports / 'comparison.json').unlink(missing_ok=True)
    command = [args.docker, 'compose', '-p', 'outcome-offline-load', '-f', str(root / 'compose.load.yaml')]

    def compose(*parts):
        return subprocess.run([*command, *parts], cwd=root, check=True)

    try:
        compose('up', '-d', '--build', '--scale', 'worker=1', 'api', 'worker')
        compose('run', '--rm', '--no-deps', 'runner', 'python', '-m', 'loadtests.run', '--label', 'workers-1')
        compose('up', '-d', '--no-deps', '--scale', 'worker=3', 'worker')
        compose('run', '--rm', '--no-deps', 'runner', 'python', '-m', 'loadtests.run', '--label', 'workers-3')
        compose('up', '-d', '--no-deps', '--scale', 'worker=1', 'worker')
        marker = reports / 'kill-ready'
        marker.unlink(missing_ok=True)
        runner = subprocess.Popen([*command, 'run', '--rm', '--no-deps', 'runner', 'python', '-m',
                                   'loadtests.run', '--label', 'recovery', '--recovery'], cwd=root)
        try:
            deadline = time.monotonic() + 60
            while not marker.exists():
                if runner.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Recovery runner did not reach an active provider call')
                time.sleep(0.05)
            compose('kill', '-s', 'SIGKILL', 'worker')
            compose('start', 'worker')
            if runner.wait(timeout=1800):
                raise RuntimeError('Recovery check failed; see recovery.json')
        finally:
            if runner.poll() is None:
                runner.terminate()
                runner.wait(timeout=10)
        one = json.loads((reports / 'workers-1.json').read_text())
        three = json.loads((reports / 'workers-3.json').read_text())
        comparison = {'passed': True, 'workers_1_jobs_per_second': one['successful_jobs_per_second'],
                      'workers_3_jobs_per_second': three['successful_jobs_per_second'],
                      'observed_throughput_ratio': three['successful_jobs_per_second'] / one['successful_jobs_per_second']}
        (reports / 'comparison.json').write_text(json.dumps(comparison, indent=2))
        print(json.dumps(comparison, indent=2))
    finally:
        # Preserve reports and volumes for diagnosis; no production stack changes.
        compose('stop')


if __name__ == '__main__':
    main()
