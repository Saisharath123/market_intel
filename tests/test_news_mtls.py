"""Verify the staged news-only client certificate path."""
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest
import yaml

from market import client


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/chart'
HELM = os.environ.get('HELM_PATH') or shutil.which('helm')
spec = importlib.util.spec_from_file_location('news_mtls_smoke', CHART / 'files/smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
NEWS = 'https://news-api.market-intel-staging.svc.cluster.local:8000'
MARKET = 'https://market-api.market-intel-staging.svc.cluster.local:8000'
CERT, KEY = '/news-client/tls.crt', '/news-client/tls.key'
ARN = 'arn:aws:acm:ap-south-1:000000000000:certificate/00000000-0000-0000-0000-000000000000'


def setup_news_client(monkeypatch, url=NEWS):
    monkeypatch.setenv('NEWS_API_URL', url)
    monkeypatch.setenv('NEWS_API_CLIENT_CERT_FILE', CERT)
    monkeypatch.setenv('NEWS_API_CLIENT_KEY_FILE', KEY)


def test_client_certificate_is_used_for_news_only(monkeypatch):
    setup_news_client(monkeypatch)
    monkeypatch.setenv('MARKET_API_URL', MARKET)
    payload = {'items': [], 'mode': 'demo', 'source': 'Demo', 'status': 'ok', 'fetched_at': 'timestamp', 'error': None}
    response = Mock(status_code=200)
    response.json.return_value = payload
    get = Mock(return_value=response)
    monkeypatch.setattr(client.requests, 'get', get)

    assert client.news() == payload
    get.assert_called_once_with(NEWS + '/api/v1/news', timeout=30, cert=(CERT, KEY), allow_redirects=False)
    get.reset_mock()
    assert client.quotes() == payload
    get.assert_called_once_with(MARKET + '/api/v1/quotes', timeout=30)


@pytest.mark.parametrize('url,missing_key', [('', False), ('http://news-api:8000', False), (NEWS, True)])
def test_configured_news_mtls_fails_closed_without_https_and_a_complete_pair(monkeypatch, url, missing_key):
    setup_news_client(monkeypatch, url)
    if missing_key:
        monkeypatch.delenv('NEWS_API_CLIENT_KEY_FILE')
    get = Mock()
    fallback = Mock()
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.service, 'news', fallback)

    assert client.news()['status'] == 'unavailable'
    get.assert_not_called()
    fallback.assert_not_called()


def test_news_mtls_rejects_redirects(monkeypatch):
    setup_news_client(monkeypatch)
    get = Mock(return_value=Mock(status_code=302))
    monkeypatch.setattr(client.requests, 'get', get)
    assert client.news()['status'] == 'unavailable'
    get.assert_called_once_with(NEWS + '/api/v1/news', timeout=30, cert=(CERT, KEY), allow_redirects=False)


def test_smoke_uses_separate_verified_opener_for_news(monkeypatch):
    visited = []
    market_opener, news_opener = object(), object()
    snap = {'mode': 'demo', 'status': 'ok', 'fetched_at': datetime.now(timezone.utc).isoformat()}
    response = {
        '/health': {'service': 'news'},
        '/api/v1/news': dict(snap, items=[{'title': 'Test headline'}]),
        '/api/v1/quotes': dict(snap, items=[{'symbol': 'NSE:TEST', 'price': 100}] * 6),
        '/api/v1/insights': {'advances': 0, 'declines': 0, 'unchanged': 0, 'leaders': []},
        '/_stcore/health': 'ok',
        '/metrics': 'market_intel_http_requests_total 1',
    }

    def fake_fetch(url, as_json=True, *, open_url=None):
        visited.append((url, open_url))
        if url == MARKET + '/health':
            return {'service': 'market'}
        return response['/' + url.split('/', 3)[-1]]

    monkeypatch.setattr(smoke, 'fetch', fake_fetch)
    smoke.check_services(MARKET, NEWS, 'http://dashboard:8501', True, open_url=market_opener, open_news_url=news_opener)
    assert {url for url, opener in visited if opener is news_opener} == {NEWS + '/health', NEWS + '/api/v1/news'}
    assert all(opener is market_opener for url, opener in visited if not url.startswith(NEWS))


def render(environment, mode='', *, tls_profile=True, prod_phase='retire'):
    if not HELM:
        pytest.skip('Helm is required for chart rendering; set HELM_PATH or install helm')
    namespace = 'market-intel-staging' if environment == 'staging' else 'market-intel'
    args = [HELM, 'template', 'market-intel', str(CHART), '-n', namespace,
            '-f', str(CHART / f'values-{environment}.yaml')]
    if tls_profile:
        args.extend(['-f', str(CHART / f'values-{environment}-tls.yaml')])
    if environment == 'production' and tls_profile:
        args.extend(['--set-string', f'internalTls.productionPhase={prod_phase}',
                     '--set-string', f'internalTls.albCertificateArn={ARN}'])
    if mode:
        args.extend(['-f', str(CHART / 'values-news-mtls.yaml'), '--set-string', f'internalTls.newsClientAuth={mode}'])
    for service in ('dashboard', 'market-api', 'news-api'):
        args.extend(['--set-string', f'services.{service}.image=registry.example/{service}@sha256:' + 'a' * 64])
    return subprocess.run(args, text=True, capture_output=True, check=False)


def docs(result):
    assert result.returncode == 0, result.stderr
    return {(doc['kind'], doc['metadata']['name']): doc for doc in yaml.safe_load_all(result.stdout) if doc}


def spec_for(doc):
    return doc['spec']['template']['spec']


@pytest.mark.parametrize('environment,port', [('staging', '8000'), ('production', '8443')])
def test_mtls_prepares_news_clients_before_changing_the_server(environment, port):
    original = docs(render(environment))
    prepare = docs(render(environment, 'prepare'))
    require = docs(render(environment, 'require'))
    news = ('Deployment', 'news-api')
    dashboard = ('Deployment', 'dashboard')
    assert prepare[news] == original[news]
    assert prepare[dashboard] == require[dashboard]
    assert 'market-intel-news-mtls-probe' not in [name for kind, name in prepare if kind == 'ConfigMap']

    for phase in (prepare, require):
        dashboard_pod = spec_for(phase[dashboard])
        assert {v['secret']['secretName'] for v in dashboard_pod['volumes'] if 'secret' in v} == {'dashboard-news-client-tls'}
        smoke_pod = spec_for(phase['Job', 'market-intel-smoke'])
        assert smoke_pod['securityContext']['fsGroup'] == 10001
        assert {v['secret']['secretName'] for v in smoke_pod['volumes'] if 'secret' in v} == {'smoke-news-client-tls'}
        news_endpoint, = phase['ServiceMonitor', 'news-api']['spec']['endpoints']
        assert news_endpoint['tlsConfig']['cert']['secret'] == {'name': 'prometheus-news-client-tls', 'key': 'tls.crt'}
        assert news_endpoint['tlsConfig']['keySecret'] == {'name': 'prometheus-news-client-tls', 'key': 'tls.key'}
        assert phase['ServiceMonitor', 'market-api'] == original['ServiceMonitor', 'market-api']

    news_pod = spec_for(require[news])
    server = news_pod['containers'][0]
    assert {entry['name']: entry['value'] for entry in server['env']}['UVICORN_SSL_CERT_REQS'] == '2'
    assert {v['secret']['secretName'] for v in news_pod['volumes'] if 'secret' in v} == {'news-api-tls', 'news-probe-client-tls'}
    for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
        assert 'httpGet' not in server[probe]
        assert server[probe]['exec']['command'][-1] == port
    probe_code = require['ConfigMap', 'market-intel-news-mtls-probe']['data']['probe.py']
    compile(probe_code, '<news readiness probe>', 'exec')
    assert "('127.0.0.1', port)" in probe_code and 'server_hostname=host' in probe_code


@pytest.mark.parametrize('environment,tls_profile,prod_phase', [('staging', False, ''), ('production', True, 'switch')])
def test_mtls_cannot_leave_a_plaintext_news_listener(environment, tls_profile, prod_phase):
    result = render(environment, 'require', tls_profile=tls_profile, prod_phase=prod_phase)
    assert result.returncode != 0
    assert 'TLS staging or retired production profile' in result.stderr
