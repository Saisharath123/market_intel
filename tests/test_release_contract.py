"""Release controls exercise wrong registries, mutable images and HTTP-200 failures."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = module_at('gitops_release', '.github/scripts/gitops_release.py')
smoke = module_at('release_smoke', 'deploy/chart/files/smoke.py')


def approved_images():
    return {
        name: f'521595303406.dkr.ecr.ap-south-1.amazonaws.com/market-intel/{repository}@sha256:' + 'a' * 64
        for name, repository in release.SERVICES.items()
    }


@pytest.mark.parametrize('bad_image', [
    '521595303406.dkr.ecr.ap-south-1.amazonaws.com/market-intel/market:latest',
    '000000000000.dkr.ecr.ap-south-1.amazonaws.com/market-intel/market@sha256:' + 'a' * 64,
    '521595303406.dkr.ecr.us-east-1.amazonaws.com/market-intel/market@sha256:' + 'a' * 64,
    '521595303406.dkr.ecr.ap-south-1.amazonaws.com/unrelated/market@sha256:' + 'a' * 64,
    '521595303406.dkr.ecr.ap-south-1.amazonaws.com/market-intel/market@sha256:bad',
])
def test_release_refuses_mutable_or_unapproved_images(tmp_path, bad_image):
    config = {'services': {name: {'image': image} for name, image in approved_images().items()}}
    config['services']['market-api']['image'] = bad_image
    source = tmp_path / 'source.yaml'
    source.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='approved ECR repository'):
        release.load_images(source, '521595303406', 'ap-south-1')


def test_promotion_preserves_reviewed_environment_and_comments(tmp_path):
    target = tmp_path / 'production.yaml'
    target.write_text('''# Production settings belong to the operator.
environment: production
services:
  dashboard:
    image: ""
    replicas: 2
  market-api:
    image: ""
    env: {MARKET_MODE: yahoo}
  news-api:
    image: ""
rollouts:
  enabled: true
''')
    before = yaml.safe_load(target.read_text())
    assert release.patch_images(target, approved_images())
    after = yaml.safe_load(target.read_text())
    assert after['environment'] == before['environment']
    assert after['rollouts'] == before['rollouts']
    assert after['services']['dashboard']['replicas'] == 2
    assert after['services']['market-api']['env'] == {'MARKET_MODE': 'yahoo'}
    assert '# Production settings belong to the operator.' in target.read_text()
    assert release.load_images(target, '521595303406', 'ap-south-1') == approved_images()
    assert release.patch_images(target, approved_images()) is False


@pytest.mark.parametrize('status', ['unavailable', 'partial', 'stale', 'unknown'])
def test_smoke_rejects_http_200_provider_failures(status):
    payload = {'status': status, 'items': [{'symbol': 'NSE:TCS'}], 'fetched_at': datetime.now(timezone.utc).isoformat()}
    with pytest.raises(ValueError, match='snapshot'):
        smoke.check_snapshot(payload, expect_demo=False)


@pytest.mark.parametrize('age_seconds', [361, -31])
def test_smoke_rejects_old_or_future_fetch_timestamps(age_seconds):
    payload = {'status': 'ok', 'mode': 'demo', 'items': [1],
               'fetched_at': (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()}
    with pytest.raises(ValueError, match='timestamp'):
        smoke.check_snapshot(payload, expect_demo=True)


def test_staging_cannot_silently_switch_to_live_provider():
    payload = {'status': 'ok', 'mode': 'yahoo', 'items': [1], 'fetched_at': datetime.now(timezone.utc).isoformat()}
    with pytest.raises(ValueError, match='deterministic demo'):
        smoke.check_snapshot(payload, expect_demo=True)
