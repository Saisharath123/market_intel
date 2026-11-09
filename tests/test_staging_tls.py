"""Verify staging TLS without changing the default HTTP release."""
import importlib.util
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/chart'
spec = importlib.util.spec_from_file_location('tls_smoke', CHART / 'files/smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
HELM = os.environ.get('HELM_PATH') or shutil.which('helm')
NAMESPACE = 'market-intel-staging'
CA = '/var/run/market-intel/ca/ca.crt'


def render(environment, *, tls=False):
    if not HELM:
        pytest.skip('Helm is required for chart rendering; set HELM_PATH or install helm')
    namespace = NAMESPACE if environment == 'staging' else 'market-intel'
    args = [HELM, 'template', 'market-intel', str(CHART), '-n', namespace,
            '-f', str(CHART / f'values-{environment}.yaml')]
    if tls:
        args.extend(['-f', str(CHART / 'values-staging-tls.yaml')])
    for service in ('dashboard', 'market-api', 'news-api'):
        args.extend(['--set-string', f'services.{service}.image=registry.example/{service}@sha256:' + 'a' * 64])
    return subprocess.run(args, text=True, capture_output=True, check=False)


def test_staging_tls_wires_the_same_hostname_and_trust_to_all_clients():
    result = render('staging', tls=True)
    assert result.returncode == 0, result.stderr
    docs = {(doc['kind'], doc['metadata']['name']): doc for doc in yaml.safe_load_all(result.stdout) if doc}

    dashboard = docs['Deployment', 'dashboard']['spec']['template']['spec']
    dashboard_env = {entry['name']: entry['value'] for entry in dashboard['containers'][0]['env']}
    assert dashboard_env['REQUESTS_CA_BUNDLE'] == CA
    assert {volume['configMap']['name'] for volume in dashboard['volumes'] if 'configMap' in volume} == {'market-intel-ca'}
    assert not any('secret' in volume for volume in dashboard['volumes'])

    job = docs['Job', 'market-intel-smoke']['spec']['template']['spec']
    args = job['containers'][0]['args']
    assert args[args.index('--tls-ca-file') + 1] == CA
    assert {volume['configMap']['name'] for volume in job['volumes']} == {'market-intel-ca', 'market-intel-smoke'}

    for name, role in (('market-api', 'market'), ('news-api', 'news')):
        dns = f'{name}.{NAMESPACE}.svc.cluster.local'
        url = f'https://{dns}:8000'
        assert dashboard_env[role.upper() + '_API_URL'] == url
        assert args[args.index(f'--{role}-url') + 1] == url
        if role == 'market':
            assert args[args.index('--market-traffic-url') + 1] == url
        pod = docs['Deployment', name]['spec']['template']['spec']
        container = pod['containers'][0]
        env = {entry['name']: entry['value'] for entry in container['env']}
        assert env['UVICORN_SSL_CERTFILE'] == '/var/run/market-intel/tls/tls.crt'
        assert env['UVICORN_SSL_KEYFILE'] == '/var/run/market-intel/tls/tls.key'
        assert 'REQUESTS_CA_BUNDLE' not in env
        assert {volume['secret']['secretName'] for volume in pod['volumes'] if 'secret' in volume} == {name + '-tls'}
        assert all(container[probe]['httpGet']['scheme'] == 'HTTPS'
                   for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'))
        endpoint, = docs['ServiceMonitor', name]['spec']['endpoints']
        assert endpoint['scheme'] == 'https'
        assert endpoint['tlsConfig'] == {'serverName': dns,
                                        'ca': {'configMap': {'name': 'market-intel-ca', 'key': 'ca.crt'}}}


def test_staging_overlay_cannot_convert_production_alb_protocol():
    result = render('production', tls=True)
    assert result.returncode != 0
    assert 'supported only for staging' in result.stderr


def test_tls_smoke_rejects_plaintext_api_urls(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['smoke.py', '--tls-ca-file', CA])
    with pytest.raises(SystemExit) as error:
        smoke.main()
    assert error.value.code == 2


def test_tls_smoke_builds_a_hostname_verified_opener(monkeypatch):
    market = 'https://market-api:8000'
    news = 'https://news-api:8000'
    monkeypatch.setattr(sys, 'argv', ['smoke.py', '--market-url', market, '--news-url', news, '--tls-ca-file', CA])
    context = ssl.create_default_context()
    create_calls, check_calls = [], []

    def create_context(*, cafile):
        create_calls.append(cafile)
        return context

    def check_services(*args, open_url):
        check_calls.append((args, open_url))

    monkeypatch.setattr(smoke.ssl, 'create_default_context', create_context)
    monkeypatch.setattr(smoke, 'check_services', check_services)
    smoke.main()

    assert create_calls == [CA]
    (args, open_url), = check_calls
    assert args[:2] == (market, news)
    handlers = open_url.__self__.handlers
    https = next(handler for handler in handlers if isinstance(handler, smoke.HTTPSHandler))
    assert https._context is context and context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert any(isinstance(handler, smoke._NoRedirect) for handler in handlers)
