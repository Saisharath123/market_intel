"""Verify migration phases retain controller-managed traffic and network boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/chart'
HELM = os.environ.get('HELM_PATH') or shutil.which('helm')
ARN = 'arn:aws:acm:ap-south-1:000000000000:certificate/00000000-0000-0000-0000-000000000000'
ANNOTATION = 'alb.ingress.kubernetes.io/'
APIS = ('market-api', 'news-api')
MARKET_SERVICES = ('market-api', 'market-api-canary', 'market-api-metrics')


def render(phase, arn=ARN):
    if not HELM:
        pytest.skip('Helm is required for chart rendering; set HELM_PATH or install helm')
    args = [HELM, 'template', 'market-intel', str(CHART), '-n', 'market-intel',
            '-f', str(CHART / 'values-production.yaml'),
            '-f', str(CHART / 'values-production-network.yaml'),
            '-f', str(CHART / 'values-production-tls.yaml'),
            '--set', 'networkHardening.enabled=true',
            '--set-string', f'internalTls.productionPhase={phase}',
            '--set-string', f'internalTls.albCertificateArn={arn}']
    for service in ('dashboard', *APIS):
        args.extend(['--set-string', f'services.{service}.image=registry.example/{service}@sha256:' + 'a' * 64])
    return subprocess.run(args, text=True, capture_output=True, check=False)


@pytest.fixture(scope='module')
def phases():
    result = {}
    for phase in ('prepare', 'switch', 'retire'):
        process = render(phase)
        assert process.returncode == 0, process.stderr
        result[phase] = {(doc['kind'], doc['metadata']['name']): doc
                         for doc in yaml.safe_load_all(process.stdout) if doc}
    return result


def pod(docs, kind, name):
    return docs[kind, name]['spec']['template']['spec']


def args(docs):
    values = pod(docs, 'Job', 'market-intel-smoke')['containers'][0]['args']
    return values


def rollout(docs):
    return docs['Rollout', 'market-api']['spec']['strategy']['canary']


def listener(docs):
    return docs['Ingress', 'market-api-canary']['metadata']['annotations']


def test_prepare_keeps_existing_http_and_warms_verified_tls(phases):
    prepared = phases['prepare']
    assert json.loads(listener(prepared)[ANNOTATION + 'listen-ports']) == [{'HTTP': 80}]
    assert ANNOTATION + 'backend-protocol' not in listener(prepared)
    assert rollout(prepared)['trafficRouting']['alb']['servicePort'] == 8000
    assert args(prepared)[args(prepared).index('--market-traffic-url') + 1].startswith('http://')
    assert args(prepared).count('--warm-tls-endpoint') == 2
    for kind, name in (('Rollout', 'market-api'), ('Deployment', 'news-api')):
        spec = pod(prepared, kind, name)
        old, new = spec['containers']
        assert old['image'] == new['image'] and '@sha256:' in new['image']
        assert old['ports'] == [{'name': 'http', 'containerPort': 8000}]
        assert new['ports'] == [{'name': 'https', 'containerPort': 8443}]
        assert new['readinessProbe']['httpGet']['scheme'] == 'HTTPS'
        assert new['securityContext']['readOnlyRootFilesystem'] is True
        assert new['resources']['requests'] == old['resources']['requests']
        assert next(v for v in spec['volumes'] if v['name'] == 'internal-tls')['secret']['secretName'] == name + '-tls'


def test_switch_keeps_both_server_listeners_without_a_new_backend_rollout(phases):
    prepared, switched = phases['prepare'], phases['switch']
    for kind, name in (('Rollout', 'market-api'), ('Deployment', 'news-api')):
        assert pod(prepared, kind, name) == pod(switched, kind, name)
    policy = listener(switched)
    assert json.loads(policy[ANNOTATION + 'listen-ports']) == [{'HTTP': 80}, {'HTTPS': 443}]
    assert policy[ANNOTATION + 'certificate-arn'] == ARN
    assert policy[ANNOTATION + 'backend-protocol'] == policy[ANNOTATION + 'healthcheck-protocol'] == 'HTTPS'
    assert rollout(switched)['trafficRouting']['alb']['servicePort'] == 8443
    assert rollout(switched)['steps'] == rollout(prepared)['steps']
    assert rollout(switched)['stableService'] == 'market-api'
    assert rollout(switched)['canaryService'] == 'market-api-canary'
    env = {var['name']: var['value'] for var in pod(switched, 'Deployment', 'dashboard')['containers'][0]['env']}
    assert env['MARKET_API_URL'] == 'https://api.market-intel.internal'
    assert env['NEWS_API_URL'] == 'https://news-api.market-intel.svc.cluster.local:8443'
    for name in APIS:
        endpoint, = switched['ServiceMonitor', name]['spec']['endpoints']
        assert endpoint['scheme'] == endpoint['port'] == 'https'
        assert endpoint['tlsConfig']['serverName'] == f'{name}.market-intel.svc.cluster.local'


@pytest.mark.parametrize('phase,service_ports,alb_ports', [
    ('prepare', [8000, 8443], [80]),
    ('switch', [8000, 8443], [80, 443]),
    ('retire', [8443], [443]),
])
def test_phases_agree_on_service_target_and_network_ports(phases, phase, service_ports, alb_ports):
    docs = phases[phase]
    for service in (*MARKET_SERVICES, 'news-api'):
        ports = docs['Service', service]['spec']['ports']
        assert [entry['port'] for entry in ports] == service_ports
        for entry in ports:
            assert entry['name'] == entry['targetPort']
    allowed_market = docs['NetworkPolicy', 'market-api-boundary']['spec']['ingress'][0]['ports']
    allowed_news = docs['NetworkPolicy', 'news-api-ingress']['spec']['ingress'][0]['ports']
    for ports in (allowed_market, allowed_news):
        assert [entry['port'] for entry in ports] == service_ports
    for name in ('dashboard-boundary', 'smoke-boundary', 'canary-traffic-boundary'):
        egress = docs['NetworkPolicy', name]['spec']['egress']
        via_alb, = [entry for entry in egress if 'ipBlock' in entry['to'][0]]
        assert [entry['port'] for entry in via_alb['ports']] == alb_ports


def test_retire_removes_plaintext_api_ports_and_http_frontend(phases):
    retired = phases['retire']
    assert json.loads(listener(retired)[ANNOTATION + 'listen-ports']) == [{'HTTPS': 443}]
    for kind, name in (('Rollout', 'market-api'), ('Deployment', 'news-api')):
        primary, = pod(retired, kind, name)['containers']
        assert primary['name'] == name
        assert primary['ports'] == [{'name': 'https', 'containerPort': 8443}]
        assert primary['readinessProbe']['httpGet']['scheme'] == 'HTTPS'
        assert primary['args'][primary['args'].index('--port') + 1] == '8443'


def test_switch_requires_explicit_imported_certificate():
    process = render('switch', arn='')
    assert process.returncode != 0
    assert 'reviewed imported certificate' in process.stderr
