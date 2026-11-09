import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
REVISION = 'a' * 40
OTHER_REVISION = 'b' * 40
IMAGE = '123456789012.dkr.ecr.ap-south-1.amazonaws.com/market-intel/news@sha256:' + 'f' * 64


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'infra'))
    import release_provenance
    return release_provenance


def completed(args, stderr=''):
    return subprocess.CompletedProcess(args, int(bool(stderr)), '', stderr)


def test_only_the_main_release_workflow_can_record(gate):
    env = {
        'GITHUB_REPOSITORY': 'example/market-intel',
        'GITHUB_REPOSITORY_ID': '12345',
        'GITHUB_SHA': REVISION,
        'GITHUB_RUN_ID': '98765',
        'GITHUB_REF': 'refs/heads/main',
        'GITHUB_WORKFLOW_REF': f'example/market-intel/{gate.BUILDER}',
    }
    assert gate.release_identity(env, True) == ('12345', REVISION, '98765')
    env['GITHUB_WORKFLOW_REF'] = 'example/market-intel/.github/workflows/untrusted.yml@refs/heads/main'
    with pytest.raises(ValueError, match='release workflow'):
        gate.release_identity(env, True)
    with pytest.raises(ValueError, match='Only verification'):
        gate.release_identity(env, True, '12345')
    assert gate.release_identity({}, False, '12345') == ('12345', None, None)


@pytest.mark.parametrize('newly_built,initial_bare_result,expected,valid', [
    (False, '', ['verify'], False),
    (True, 'signature revoked', ['verify', 'verify'], False),
    (True, 'no signature is associated', ['verify', 'verify', 'sign', 'verify'], True),
    (True, '', ['verify', 'verify', 'sign', 'verify'], True),
])
def test_fresh_build_can_sign_but_reuse_cannot_invent_provenance(
    gate, monkeypatch, newly_built, initial_bare_result, expected, valid,
):
    calls = []
    args = SimpleNamespace(notation='notation', sign_new_service=['news-api'] if newly_built else [])

    def run(command, _env):
        calls.append(command)
        assert command[-1] == IMAGE
        if len(calls) == 1:
            assert f'market-intel.source-revision={REVISION}' in command
            assert 'market-intel.source-repository-id=12345' in command
            assert f'market-intel.builder={gate.BUILDER}' in command
            return completed(command, 'required signed metadata missing')
        if len(calls) == 2:
            return completed(command, initial_bare_result)
        if command[1] == 'sign':
            assert 'market-intel.build-run-id=98765' in command
            assert 'exact-profile' in command
            assert '--force-referrers-tag=false' in command
        return completed(command)

    monkeypatch.setattr(gate, 'run', run)
    if valid:
        gate.verify_release(args, {'news-api': IMAGE}, '12345', REVISION, '98765', 'exact-profile', {})
    else:
        with pytest.raises(ValueError, match='signature gate is closed'):
            gate.verify_release(args, {'news-api': IMAGE}, '12345', REVISION, '98765', 'exact-profile', {})
    assert [call[1] for call in calls] == expected


def test_staged_shared_tags_are_checked_cryptographically(gate, monkeypatch):
    revisions = []

    def verify(_args, _images, _repository_id, revision, _run_id, _profile, _env):
        revisions.append(revision)
        if revision == REVISION:
            raise ValueError('older tag does not have matching build metadata')

    monkeypatch.setattr(gate, 'verify_release', verify)
    gate.verify_staged_revisions(None, {}, '12345', {REVISION, OTHER_REVISION}, 'exact-profile', {})
    assert revisions == [REVISION, OTHER_REVISION]
    with pytest.raises(ValueError, match='No common staged source revision'):
        gate.verify_staged_revisions(None, {}, '12345', {REVISION}, 'exact-profile', {})


def test_release_and_promotion_gate_before_gitops_and_scope_aws_permissions():
    for path, job, gate, proposal in (
        ('eks-images.yml', 'release', 'Record private build provenance', 'Propose staging GitOps release'),
        ('promote-production.yml', 'promote', 'Require valid unrevoked signatures', 'Create the reviewed production promotion PR'),
    ):
        workflow = yaml.safe_load((ROOT / '.github/workflows' / path).read_text(encoding='utf-8'))
        steps = workflow['jobs'][job]['steps']
        names = [step.get('name', '') for step in steps]
        gate_index = next(i for i, name in enumerate(names) if name.startswith(gate))
        assert gate_index < names.index(proposal)
    aws = next(step for step in steps if step.get('uses', '').startswith('aws-actions/configure-aws-credentials@'))
    session = json.loads(aws['with']['inline-session-policy'])
    actions = {action for rule in session['Statement']
               for action in ([rule['Action']] if isinstance(rule['Action'], str) else rule['Action'])}
    assert 'signer:GetRevocationStatus' in actions
    assert 'signer:SignPayload' not in actions and 'ecr:PutImage' not in actions
    template = json.loads((ROOT / 'infra/cloudformation/ci.json').read_text(encoding='utf-8'))
    publisher = next(resource for resource in template['Resources'].values() if resource['Type'] == 'AWS::IAM::Role')
    statements = publisher['Properties']['Policies'][0]['PolicyDocument']['Statement']
    signing = next(item for item in statements if item['Action'] == 'signer:SignPayload')
    assert signing['Resource']['Fn::Sub'].endswith('/signing-profiles/MarketIntelReleaseSigning')
