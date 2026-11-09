"""Verify the opt-in network boundaries and staged load-balancer move."""

from ipaddress import ip_network
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
DNS_RULE = {
    'to': [{
        'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
        'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}},
    }],
    'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}],
}
ALB_SUBNETS = 'alb.ingress.kubernetes.io/subnets'


def render(environment, *overlays, activate=False):
    if not HELM:
        pytest.skip('Helm is required for chart rendering; set HELM_PATH or install helm')
    namespace = 'market-intel' if environment == 'production' else 'market-intel-staging'
    args = [HELM, 'template', 'market-intel', str(CHART), '--namespace', namespace,
            '-f', str(CHART / f'values-{environment}.yaml')]
    for overlay in overlays:
        args.extend(['-f', str(CHART / overlay)])
    if activate:
        args.extend(['--set', 'networkHardening.enabled=true'])
    return subprocess.run(args, text=True, capture_output=True, check=False)


def resources(result, kind):
    assert result.returncode == 0, result.stderr
    return {doc['metadata']['name']: doc for doc in yaml.safe_load_all(result.stdout) if doc and doc['kind'] == kind}


def service_egress(app, port):
    return {
        'to': [{'podSelector': {'matchLabels': {'app': app}}}],
        'ports': [{'protocol': 'TCP', 'port': port}],
    }


def test_staging_isolates_private_dashboard_and_limits_demo_service_egress():
    result = render('staging', 'values-staging-network.yaml')
    policies = resources(result, 'NetworkPolicy')
    assert set(policies) == {
        'dashboard-boundary', 'market-api-ingress', 'news-api-ingress',
        'market-api-boundary', 'news-api-boundary', 'smoke-boundary',
    }
    dashboard = policies['dashboard-boundary']['spec']
    assert dashboard['policyTypes'] == ['Ingress', 'Egress']
    assert dashboard['ingress'] == [{
        'from': [{'podSelector': {'matchLabels': {'app': 'market-intel-smoke'}}}],
        'ports': [{'protocol': 'TCP', 'port': 8501}],
    }]
    assert dashboard['egress'] == [DNS_RULE, service_egress('news-api', 8000), service_egress('market-api', 8000)]
    for api in ('market-api', 'news-api'):
        assert policies[f'{api}-boundary']['spec'] == {
            'podSelector': {'matchLabels': {'app': api}},
            'policyTypes': ['Egress'],
            'egress': [DNS_RULE],
        }
    assert policies['smoke-boundary']['spec']['egress'] == [
        DNS_RULE, service_egress('dashboard', 8501), service_egress('market-api', 8000),
        service_egress('news-api', 8000),
    ]


def test_production_overlay_moves_only_alb_before_policy_activation():
    baseline = render('production')
    move = render('production', 'values-production-network.yaml')
    base_ingress = resources(baseline, 'Ingress')['market-api-canary']
    moved_ingress = resources(move, 'Ingress')['market-api-canary']
    assert ALB_SUBNETS not in base_ingress['metadata']['annotations']
    assert moved_ingress['metadata']['annotations'][ALB_SUBNETS] == 'market-intel-sre-alb-a,market-intel-sre-alb-b'
    assert resources(baseline, 'NetworkPolicy') == resources(move, 'NetworkPolicy')


def test_production_activation_keeps_alb_health_smoke_metrics_and_provider_https():
    result = render('production', 'values-production-network.yaml', activate=True)
    policies = resources(result, 'NetworkPolicy')
    expected_alb = [{'ipBlock': {'cidr': cidr}} for cidr in ('10.80.20.0/27', '10.80.21.0/27')]
    alb_egress = {'to': expected_alb, 'ports': [{'protocol': 'TCP', 'port': 80}]}
    market = policies['market-api-boundary']['spec']
    assert market['ingress'] == [{
        'from': expected_alb + [
            {'podSelector': {'matchLabels': {'app': 'market-intel-smoke'}}},
            {
                'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}},
                'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus'}},
            },
        ],
        'ports': [{'protocol': 'TCP', 'port': 8000}],
    }]
    assert policies['dashboard-boundary']['spec']['egress'] == [DNS_RULE, service_egress('news-api', 8000), alb_egress]
    assert policies['smoke-boundary']['spec']['egress'] == [
        DNS_RULE, service_egress('dashboard', 8501), service_egress('market-api', 8000),
        service_egress('news-api', 8000), alb_egress,
    ]
    for api in ('market-api', 'news-api'):
        dns, internet = policies[f'{api}-boundary']['spec']['egress']
        assert dns == DNS_RULE
        assert internet['ports'] == [{'protocol': 'TCP', 'port': 443}]
        (external,) = internet['to']
        assert external['ipBlock']['cidr'] == '0.0.0.0/0'
        assert {'10.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16'} <= set(
            external['ipBlock']['except'])


def test_production_policy_rejects_activation_without_dedicated_alb_values():
    result = render('production', activate=True)
    assert result.returncode != 0
    assert 'requires the weighted ALB route and two dedicated ALB subnet names and CIDRs' in result.stderr


def test_alb_subnets_are_isolated_and_match_helm_configuration():
    template = json.loads((ROOT / 'infra/cloudformation/eks.json').read_text())
    resources = template['Resources']
    assert template['Parameters']['EnableDedicatedAlbSubnets']['Default'] == 'false'
    hardening = yaml.safe_load((CHART / 'values-production-network.yaml').read_text())['networkHardening']
    workers = [resources[name]['Properties'] for name in ('Private0', 'Private1')]
    alb = [resources[name]['Properties'] for name in ('AlbSubnet0', 'AlbSubnet1')]
    assert [subnet['CidrBlock'] for subnet in alb] == hardening['albSubnetCidrs']
    assert [subnet['Tags'][0]['Value'] for subnet in alb] == hardening['albSubnetNames']
    assert [subnet['AvailabilityZone'] for subnet in alb] == [subnet['AvailabilityZone'] for subnet in workers]
    vpc = ip_network(resources['Vpc']['Properties']['CidrBlock'])
    other_cidrs = [ip_network(resources[name]['Properties']['CidrBlock'])
                   for name in ('Public0', 'Public1', 'Private0', 'Private1')]
    for i, subnet in enumerate(alb):
        cidr = ip_network(subnet['CidrBlock'])
        assert cidr.prefixlen <= 27 and cidr.subnet_of(vpc)
        assert not any(cidr.overlaps(other) for other in other_cidrs)
        assert subnet['MapPublicIpOnLaunch'] is False
        association = resources[f'AlbSubnetAssoc{i}']
        assert association['Condition'] == 'UseDedicatedAlbSubnets'
        assert association['Properties'] == {
            'SubnetId': {'Ref': f'AlbSubnet{i}'}, 'RouteTableId': {'Ref': 'AlbIsolatedRoutes'},
        }
    assert not ip_network(alb[0]['CidrBlock']).overlaps(ip_network(alb[1]['CidrBlock']))
    assert not any(resource['Type'] == 'AWS::EC2::Route' and
                   resource['Properties']['RouteTableId'] == {'Ref': 'AlbIsolatedRoutes'}
                   for resource in resources.values())
