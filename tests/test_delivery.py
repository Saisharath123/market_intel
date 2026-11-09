"""Check routed production requests and exercise rendered deployment boundaries."""
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('delivery_smoke', ROOT / 'deploy/chart/files/smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
HELM = os.environ.get('HELM_PATH') or shutil.which('helm')


def snapshot(mode, items):
    return {'status': 'ok', 'mode': mode, 'items': items,
            'fetched_at': datetime.now(timezone.utc).isoformat()}


def test_production_checks_user_api_through_weighted_route(monkeypatch):
    responses = {
        'http://market-api:8000/health': {'service': 'market'},
        'http://news-api:8000/health': {'service': 'news'},
        'http://api.market-intel.internal/api/v1/quotes': snapshot('yahoo', [{'symbol': 'NSE:TCS', 'price': 10}]),
        'http://api.market-intel.internal/api/v1/insights': {
            'advances': 1, 'declines': 0, 'unchanged': 0, 'leaders': []},
        'http://news-api:8000/api/v1/news': snapshot('cnbctv18', [{'title': 'A market headline'}]),
        'http://dashboard:8501/_stcore/health': 'ok',
        'http://market-api:8000/metrics': 'market_intel_http_requests_total 1',
    }
    visited = []

    def fetch(url, as_json=True):
        visited.append(url)
        return responses[url]

    monkeypatch.setattr(smoke, 'fetch', fetch)
    smoke.check_services('http://market-api:8000', 'http://news-api:8000', 'http://dashboard:8501', False,
                         'http://api.market-intel.internal', 'yahoo', 'cnbctv18')
    assert set(visited) == set(responses)


def test_production_smoke_rejects_accidental_demo_fallback():
    with pytest.raises(ValueError, match='provider mode'):
        smoke.check_snapshot(snapshot('demo', [{'symbol': 'NSE:TCS', 'price': 10}]), False, 'yahoo')


def test_smoke_waits_through_bootstrap_dns_failure(monkeypatch):
    calls = []

    def check_services(*args):
        calls.append(args)
        if len(calls) < 3:
            raise OSError('Private ALB DNS record is not available yet')

    monkeypatch.setattr(smoke, 'check_services', check_services)
    monkeypatch.setattr(smoke.time, 'sleep', lambda _: None)
    monkeypatch.setattr(sys, 'argv', ['smoke.py', '--attempts', '3'])
    smoke.main()
    assert len(calls) == 3


def test_smoke_never_succeeds_without_an_attempt(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['smoke.py', '--attempts', '0'])
    with pytest.raises(SystemExit) as error:
        smoke.main()
    assert error.value.code == 2


def render(environment, *settings):
    if not HELM:
        pytest.skip('Helm is required for chart rendering; set HELM_PATH or install helm')
    args = [HELM, 'template', 'market-intel', str(ROOT / 'deploy/chart'),
            '--namespace', 'market-intel' if environment == 'production' else 'market-intel-staging',
            '-f', str(ROOT / f'deploy/chart/values-{environment}.yaml')]
    for service in ('dashboard', 'market-api', 'news-api'):
        args.extend(['--set-string', f'services.{service}.image=registry.example/{service}@sha256:' + 'a' * 64])
    for setting in settings:
        args.extend(['--set-string', setting])
    return subprocess.run(args, text=True, capture_output=True, check=False)


def documents(result):
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.mark.parametrize('environment,protected_apis', [
    ('staging', {'market-api', 'news-api'}),
    ('production', {'news-api'}),
])
def test_network_policy_limits_api_ingress_to_approved_callers(environment, protected_apis):
    docs = documents(render(environment))
    policies = [doc['spec'] for doc in docs if doc['kind'] == 'NetworkPolicy']
    assert {policy['podSelector']['matchLabels']['app'] for policy in policies} == protected_apis
    for policy in policies:
        assert policy['policyTypes'] == ['Ingress']
        assert policy['ingress'] == [{
            'from': [
                {'podSelector': {'matchLabels': {'app': 'dashboard'}}},
                {'podSelector': {'matchLabels': {'app': 'market-intel-smoke'}}},
                {
                    'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}},
                    'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus'}},
                },
            ],
            'ports': [{'protocol': 'TCP', 'port': 8000}],
        }]


@pytest.mark.parametrize('environment,expected_url,market_mode', [
    ('staging', 'http://market-api:8000', 'demo'),
    ('production', 'http://api.market-intel.internal', 'yahoo'),
])
def test_website_and_smoke_share_environment_route(environment, expected_url, market_mode):
    docs = documents(render(environment))
    dashboard = next(doc for doc in docs if doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'dashboard')
    env = dashboard['spec']['template']['spec']['containers'][0]['env']
    assert [entry['value'] for entry in env if entry['name'] == 'MARKET_API_URL'] == [expected_url]
    job = next(doc for doc in docs if doc['kind'] == 'Job' and doc['metadata']['name'] == 'market-intel-smoke')
    args = job['spec']['template']['spec']['containers'][0]['args']
    assert args[args.index('--market-traffic-url') + 1] == expected_url
    assert args[args.index('--market-mode') + 1] == market_mode
    assert ('--expect-demo' in args) == (environment == 'staging')
    assert args[args.index('--attempts') + 1] == ('60' if environment == 'production' else '6')
    assert job['spec']['activeDeadlineSeconds'] == (600 if environment == 'production' else 180)


@pytest.mark.parametrize('hostname', ['', 'https://wrong.example/path'])
def test_production_rejects_missing_or_url_instead_of_hostname(hostname):
    result = render('production', 'rollouts.apiHostname=' + hostname)
    assert result.returncode != 0
    assert 'rollouts.apiHostname' in result.stderr


def test_abort_drill_creates_new_revision_without_changing_tested_images():
    normal = documents(render('production'))
    drill = documents(render('production', 'rollouts.abortDrillId=reviewed-lab-drill-1'))
    before = next(doc for doc in normal if doc['kind'] == 'Rollout')
    after = next(doc for doc in drill if doc['kind'] == 'Rollout')
    assert before['spec']['template']['spec'] == after['spec']['template']['spec']
    assert before['spec']['template']['metadata'] != after['spec']['template']['metadata']
    template = next(doc for doc in drill if doc['kind'] == 'AnalysisTemplate')
    gate = next(metric for metric in template['spec']['metrics'] if metric['name'] == 'controlled-abort-drill')
    assert gate['provider']['prometheus']['query'] == 'vector(0)'
    assert gate['successCondition'] == 'len(result) == 1 && result[0] == 1'
    assert gate['failureLimit'] == 0
