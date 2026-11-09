import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml


SCRIPTS = Path(__file__).resolve().parents[1] / 'infra'
ACCOUNT = '123456789012'
REGION = 'ap-south-1'
PROFILE = 'MarketIntelRelease'
IMAGE = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/market-intel/news@sha256:' + 'a' * 64


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location('image_signature_gate', SCRIPTS / 'image_signature_gate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('environment,namespace', [('staging', 'market-intel-staging'), ('production', 'market-intel')])
def test_manifest_is_exact_digest_and_namespace_scoped(gate, environment, namespace):
    policy, binding = yaml.safe_load_all(gate.admission_manifest(environment, [IMAGE, IMAGE]))
    assert policy['metadata']['name'] == binding['spec']['policyName']
    assert policy['spec']['failurePolicy'] == 'Fail'
    assert json.loads(policy['spec']['variables'][0]['expression']) == [IMAGE]
    assert policy['spec']['matchConstraints']['resourceRules'][0]['resources'] == ['pods', 'pods/ephemeralcontainers']
    assert binding['spec']['validationActions'] == ['Deny']
    assert binding['spec']['matchResources']['namespaceSelector']['matchLabels'] == {
        'kubernetes.io/metadata.name': namespace,
    }


def test_unsigned_image_never_writes_an_allowlist(gate, monkeypatch, tmp_path):
    output = tmp_path / 'allowlist.yaml'
    root_cert = tmp_path / 'root.cert'
    root_cert.touch()
    monkeypatch.setattr(gate, 'load_images', lambda *_: {'news-api': IMAGE})
    monkeypatch.setattr('sys.argv', [
        'image_signature_gate.py', '--account', ACCOUNT, '--profile', PROFILE,
        '--environment', 'staging', '--source', 'staging.yaml', '--root-cert', str(root_cert),
        '--notation-libexec', str(tmp_path), '--manifest-output', str(output),
    ])

    def run(args, env=None):
        assert 'sign' not in args
        if 'get-caller-identity' in args:
            return subprocess.CompletedProcess(args, 0, ACCOUNT, '')
        if 'get-login-password' in args:
            return subprocess.CompletedProcess(args, 0, 'fake-token', '')
        if 'verify' in args:
            return subprocess.CompletedProcess(args, 1, '', 'no signature is associated')
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(gate, 'run', run)
    with pytest.raises(SystemExit, match='Signature verification failed'):
        gate.main()
    assert not output.exists()


@pytest.mark.parametrize('first_verification,expected_actions,success', [
    ('', ['verify'], True),
    ('no signature is associated', ['verify', 'sign', 'verify'], True),
    ('signature verification failed: signer mismatch', ['verify'], False),
])
def test_signing_is_idempotent_and_refuses_ambiguous_failures(
    gate, monkeypatch, tmp_path, first_verification, expected_actions, success,
):
    root_cert = tmp_path / 'root.cert'
    root_cert.touch()
    profile_arn = gate.signing_profile(ACCOUNT, REGION, PROFILE)
    config = SimpleNamespace(
        aws='aws', notation='notation', region=REGION, account=ACCOUNT,
        root_cert=root_cert, notation_libexec=tmp_path, sign_unsigned=True,
    )
    actions = []

    def run(args, env=None):
        if 'get-caller-identity' in args:
            return subprocess.CompletedProcess(args, 0, ACCOUNT, '')
        if 'get-login-password' in args:
            return subprocess.CompletedProcess(args, 0, 'fake-token', '')
        assert 'fake-token' not in args
        assert env['NOTATION_CONFIG'] == str(tmp_path / 'notation')
        if args[1] == 'policy':
            policy = json.loads(Path(args[-1]).read_text())['trustPolicies'][0]
            assert policy['registryScopes'] == [IMAGE.split('@')[0]]
            assert policy['trustedIdentities'] == [profile_arn]
            assert policy['signatureVerification'] == {'level': 'strict'}
        if args[1] in ('verify', 'sign'):
            actions.append(args[1])
            assert args[-1] == IMAGE
            assert env['NOTATION_PASSWORD'] == 'fake-token'
            if args[1] == 'sign':
                assert args[3] == gate.PLUGIN
                assert args[5] == profile_arn
            if actions == ['verify'] and first_verification:
                return subprocess.CompletedProcess(args, 1, '', first_verification)
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(gate, 'run', run)
    if success:
        gate.verify_images(config, [IMAGE], profile_arn, tmp_path)
    else:
        with pytest.raises(ValueError, match='Signature verification failed'):
            gate.verify_images(config, [IMAGE], profile_arn, tmp_path)
    assert actions == expected_actions
