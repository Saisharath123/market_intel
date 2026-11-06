#!/usr/bin/env python3
"""Connect local/CI scanners to private SonarQube using a port-scoped SSM session.

No Sonar or AWS secret is written by this helper. The log contains Session
Manager connection status only; request payloads and credentials are not logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PLUGIN_VERSION = '1.2.896.0'
PLUGIN_SHA256 = '53f51fcc67314a4bf85888a38a3967cd41f8d0f28a60a9469a63acf935ba713e'
PLUGIN_URL = ('https://s3.amazonaws.com/session-manager-downloads/plugin/'
              + PLUGIN_VERSION + '/ubuntu_64bit/session-manager-plugin.deb')
ROOT = Path(__file__).resolve().parents[1]
SESSION_RE = re.compile(r'Starting session with SessionId:\s*([A-Za-z0-9_.:@-]+)')


def install_plugin():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('Automatic install supports Ubuntu x86_64 CI only. Install the official AWS plugin locally.')
    if shutil.which('session-manager-plugin'):
        version = subprocess.check_output(['session-manager-plugin', '--version'], text=True).strip()
        if version == PLUGIN_VERSION:
            print('Pinned Session Manager plugin is already installed.')
            return
    with tempfile.TemporaryDirectory(prefix='market-intel-ssm-') as directory:
        package = Path(directory) / 'session-manager-plugin.deb'
        with urllib.request.urlopen(PLUGIN_URL, timeout=60) as response:
            package.write_bytes(response.read())
        if hashlib.sha256(package.read_bytes()).hexdigest() != PLUGIN_SHA256:
            raise RuntimeError('Official Session Manager plugin checksum mismatch; nothing was installed.')
        subprocess.run(['sudo', '-n', 'dpkg', '-i', str(package)], check=True, capture_output=True)
    print('Installed and verified the pinned AWS Session Manager plugin.')


def aws(*arguments):
    command = ['aws', *arguments, '--region', os.environ.get('AWS_REGION', 'ap-south-1'), '--no-cli-pager']
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if result.returncode:
        # AWS failure text can carry connection diagnostics, but not dump an
        # entire inherited environment or the Sonar credential.
        raise RuntimeError('AWS Session Manager operation failed: ' + result.stderr[-1500:].strip())
    return result.stdout


def session_from_log(path):
    content = path.read_text(encoding='utf-8', errors='replace') if path.exists() else ''
    match = SESSION_RE.search(content)
    return match.group(1) if match else None


def stop(runtime):
    state = runtime / 'sonar-ssm-session.json'
    if not state.exists():
        print('No SonarQube session to close.')
        return
    record = json.loads(state.read_text(encoding='utf-8'))
    session = record.get('session') or session_from_log(runtime / 'sonar-ssm-session.log')
    if session:
        aws('ssm', 'terminate-session', '--session-id', session)
    state.unlink()
    print('Closed the private SonarQube connection.')


def start(runtime, timeout):
    instance = os.environ.get('SONAR_SSM_INSTANCE_ID', '')
    document = os.environ.get('SONAR_SSM_DOCUMENT', 'market-intel-sonar-port-forward')
    if not re.fullmatch(r'i-[0-9a-f]{17}', instance):
        raise RuntimeError('Set SONAR_SSM_INSTANCE_ID to the owned Sonar EC2 instance ID.')
    if document != 'market-intel-sonar-port-forward':
        raise RuntimeError('Only the reviewed, port-9000 Session Manager document is accepted.')
    if os.environ.get('SONAR_HOST_URL', 'http://127.0.0.1:9000') != 'http://127.0.0.1:9000':
        raise RuntimeError('Private Sonar scans require SONAR_HOST_URL=http://127.0.0.1:9000.')
    if not shutil.which('session-manager-plugin'):
        raise RuntimeError('Install the AWS Session Manager plugin before starting this connection.')
    # Refuse an unrelated local listener instead of mistakenly treating its
    # health response as evidence that this AWS connection succeeded.
    with socket.socket() as check:
        try:
            check.bind(('127.0.0.1', 9000))
        except OSError:
            raise RuntimeError('Local port 9000 is occupied; close the existing listener before connecting.') from None
    runtime.mkdir(parents=True, exist_ok=True)
    state = runtime / 'sonar-ssm-session.json'
    if state.exists():
        raise RuntimeError('A prior session record exists; run stop before starting another session.')
    log_path = runtime / 'sonar-ssm-session.log'
    command = ['aws', 'ssm', 'start-session', '--target', instance, '--document-name', document,
               '--parameters', json.dumps({'localPortNumber': ['9000']}),
               '--region', os.environ.get('AWS_REGION', 'ap-south-1')]
    options = {'start_new_session': True} if os.name != 'nt' else {
        'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    state.write_text(json.dumps({'instance': instance, 'session': None}), encoding='utf-8')
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            session = session_from_log(log_path)
            log_content = log_path.read_text(encoding='utf-8', errors='replace')
            if session and f'Port 9000 opened for sessionId {session}' in log_content:
                state.write_text(json.dumps({'instance': instance, 'session': session}), encoding='utf-8')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:9000/api/system/status', timeout=5) as response:
                        if json.load(response).get('status') == 'UP':
                            print('Private SonarQube is UP at http://127.0.0.1:9000.')
                            return
                except (OSError, ValueError, urllib.error.URLError):
                    pass
            if process.poll() is not None:
                raise RuntimeError('Session Manager exited before SonarQube became ready. Inspect its status log.')
            time.sleep(3)
        raise RuntimeError('Timed out waiting for private SonarQube to become UP.')
    except BaseException:
        try:
            stop(runtime)
        finally:
            if process.poll() is None:
                process.terminate()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install-plugin', 'start', 'stop'))
    parser.add_argument('--runtime-dir', type=Path,
                        default=Path(os.environ.get('RUNNER_TEMP', str(ROOT.parent / 'market-intel-runtime'))))
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    runtime = args.runtime_dir.resolve()
    if runtime == ROOT or runtime.is_relative_to(ROOT):
        raise RuntimeError('Keep runtime state outside the Git checkout.')
    if args.action == 'install-plugin':
        install_plugin()
    elif args.action == 'start':
        start(runtime, args.timeout)
    else:
        stop(runtime)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
