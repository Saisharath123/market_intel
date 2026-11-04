"""No-network regression tests for infrastructure safety boundaries."""

import base64
import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location('market_intel_provision',
                                            Path(__file__).resolve().parents[1] / 'infra/provision.py')
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


def operator(tmp_path, *arguments):
    args = provision.parser().parse_args(['controllers-bootstrap', '--runtime-dir', str(tmp_path), *arguments])
    return provision.Provisioner(args)


def write_release(path, suffix='a'):
    lines = ['services:']
    for service, repository in provision.ECR_REPOS.items():
        image = f'{provision.ACCOUNT}.dkr.ecr.{provision.REGION}.amazonaws.com/{repository}@sha256:{suffix * 64}'
        lines += [f'  {service}:', f'    image: {image}']
    path.write_text('\n'.join(lines) + '\n')


def test_default_dryrun_never_spawns_commands(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A dry run must not invoke a subprocess, including AWS reads.')
    monkeypatch.setattr(provision.subprocess, 'run', forbidden)
    op = operator(tmp_path)
    op.controllers_bootstrap()
    op.gitops()
    op.harden_cni()
    op.args.admin_cidr = '1.2.3.4/32'
    op.foundations()
    assert not (tmp_path / 'kubeconfig').exists()


@pytest.mark.parametrize('cidr', ['0.0.0.0/0', '10.0.0.1/32', '1.2.3.4/24', '999.1.1.1/32', '::1/128'])
def test_administrator_cidr_is_a_single_public_ipv4(cidr):
    with pytest.raises(provision.argparse.ArgumentTypeError):
        provision.public_cidr(cidr)


def test_runtime_cannot_be_saved_in_git_checkout():
    with pytest.raises(provision.ProvisionError, match='outside the Git'):
        provision.checked_runtime(provision.ROOT / 'runtime')


def test_exact_principal_is_required_even_in_expected_account(tmp_path, monkeypatch):
    op = operator(tmp_path, '--execute')
    monkeypatch.setattr(op, 'aws', lambda *args, **kwargs: {'Account': provision.ACCOUNT,
                                                         'Arn': f'arn:aws:iam::{provision.ACCOUNT}:user/Other'})
    with pytest.raises(provision.ProvisionError, match='exact principal'):
        op.identity()


def test_untagged_existing_stack_cannot_be_adopted(tmp_path, monkeypatch):
    op = operator(tmp_path, '--execute')
    monkeypatch.setattr(op, 'aws', lambda *args, **kwargs: {'Stacks': [{'StackStatus': 'CREATE_COMPLETE',
                                                                     'Tags': [{'Key': 'Project', 'Value': 'market-intel'}]}]})
    with pytest.raises(provision.ProvisionError, match='ownership tags'):
        op.describe_stack(provision.STACKS['eks'])


def test_access_denied_is_not_treated_as_an_absent_stack(tmp_path, monkeypatch):
    op = operator(tmp_path)
    def denied(*args, **kwargs):
        raise provision.ProvisionError('AccessDenied: not authorized')
    monkeypatch.setattr(op, 'aws', denied)
    with pytest.raises(provision.ProvisionError, match='AccessDenied'):
        op.describe_stack('example')


def test_release_requires_digests_in_each_service_specific_repository(tmp_path):
    path = tmp_path / 'values.yaml'
    write_release(path)
    assert set(provision.release_images(path)) == set(provision.ECR_REPOS)
    path.write_text(path.read_text().replace('market-intel/news@', 'market-intel/dashboard@'))
    with pytest.raises(provision.ProvisionError, match='approved digest'):
        provision.release_images(path)


def test_release_rejects_duplicate_image_and_mutable_tag(tmp_path):
    path = tmp_path / 'values.yaml'
    write_release(path)
    path.write_text(path.read_text().replace('@sha256:' + 'a' * 64, ':latest'))
    with pytest.raises(provision.ProvisionError, match='approved digest'):
        provision.release_images(path)
    write_release(path)
    path.write_text(path.read_text() + '    image: example:latest\n')
    with pytest.raises(provision.ProvisionError, match='duplicate'):
        provision.release_images(path)


def test_boolean_chart_flags_are_not_rendered_as_truthy_strings(tmp_path, monkeypatch):
    op = operator(tmp_path)
    calls = []
    monkeypatch.setattr(op, 'run', lambda command, **kwargs: calls.append(command) or '')
    op.install_chart('argo-rollouts', settings=('dashboard.enabled=false', 'serviceAccount.name=argo-rollouts'))
    command = calls[-1]
    assert command[command.index('dashboard.enabled=false') - 1] == '--set'
    assert command[command.index('serviceAccount.name=argo-rollouts') - 1] == '--set-string'


def test_replacing_resources_leaves_change_set_unexecuted(tmp_path, monkeypatch):
    op = operator(tmp_path, '--execute')
    writes = []
    monkeypatch.setattr(op, 'describe_stack', lambda name: {'StackStatus': 'CREATE_COMPLETE', 'Tags': [], 'Parameters': []})
    def aws(*parts, **kwargs):
        if kwargs.get('write'):
            writes.append(parts)
        if parts[:2] == ('cloudformation', 'describe-change-set'):
            return {'Status': 'CREATE_COMPLETE', 'Changes': [{'ResourceChange': {
                'Action': 'Modify', 'LogicalResourceId': 'Cluster', 'Replacement': 'True'}}]}
        return {}
    monkeypatch.setattr(op, 'aws', aws)
    with pytest.raises(provision.ProvisionError, match='left unexecuted'):
        op.upsert_stack('controllers', {'ClusterName': provision.CLUSTER})
    assert any(parts[1] == 'create-change-set' for parts in writes)
    assert not any(parts[1] == 'execute-change-set' for parts in writes)


def test_prior_complete_state_does_not_complete_new_operation(tmp_path, monkeypatch):
    op = operator(tmp_path)
    stack = {'StackStatus': 'UPDATE_COMPLETE'}
    monkeypatch.setattr(op, 'describe_stack', lambda name: stack)
    monkeypatch.setattr(provision.time, 'sleep', lambda seconds: None)
    responses = iter([{'StackEvents': []}, {'StackEvents': [{
        'ClientRequestToken': 'this-operation', 'ResourceType': 'AWS::CloudFormation::Stack',
        'ResourceStatus': 'UPDATE_COMPLETE'}]}])
    monkeypatch.setattr(op, 'aws', lambda *args, **kwargs: next(responses))
    assert op.wait_stack('example', token='this-operation') is stack


def test_rollback_completion_is_failure_not_a_successful_release(tmp_path, monkeypatch):
    op = operator(tmp_path)
    monkeypatch.setattr(op, 'describe_stack', lambda name: {'StackStatus': 'UPDATE_ROLLBACK_COMPLETE'})
    monkeypatch.setattr(op, 'aws', lambda *args, **kwargs: {'StackEvents': []})
    with pytest.raises(provision.ProvisionError, match='UPDATE_ROLLBACK_COMPLETE'):
        op.wait_stack('example', token='this-operation')


def test_cf_cni_parameter_preserves_managed_ownership():
    template = json.loads((provision.ROOT / 'infra/cloudformation/eks.json').read_text())
    assert template['Parameters']['BootstrapNodeCniPolicy']['Default'] == 'true'
    assert template['Resources']['NodeRole']['Properties']['ManagedPolicyArns'][-1]['Fn::If'][0] == 'AttachNodeCniPolicy'


def test_rollouts_values_are_installed_after_monitoring_crds(tmp_path, monkeypatch):
    op = operator(tmp_path)
    operations = []
    monkeypatch.setattr(op, 'install_chart', lambda release, **kwargs: operations.append((release, kwargs)))
    monkeypatch.setattr(op, 'kubectl_run', lambda *parts, **kwargs: operations.append(('kubectl', parts)) or '')
    op.controllers_bootstrap()
    monitor = next(i for i, item in enumerate(operations) if item[0] == 'monitoring')
    rollouts = next(i for i, item in enumerate(operations) if item[0] == 'argo-rollouts')
    established = next(i for i, item in enumerate(operations)
                       if item[0] == 'kubectl' and 'crd/servicemonitors.monitoring.coreos.com' in item[1])
    assert monitor < established < rollouts
    assert operations[rollouts][1]['values'] == 'deploy/rollouts/controller-values.yaml'


def test_new_grafana_password_uses_stdin_and_is_never_logged(tmp_path, monkeypatch, capsys):
    op = operator(tmp_path, '--execute')
    calls = []
    def kubectl(*parts, **kwargs):
        calls.append((parts, kwargs))
        return ''
    monkeypatch.setattr(op, 'kubectl_run', kubectl)
    monkeypatch.setattr(provision.secrets, 'token_urlsafe', lambda size: 'private-random-password')
    op.ensure_grafana_admin()
    created = json.loads(calls[-1][1]['data'])
    assert calls[-1][0] == ('create', '-f', '-')
    assert created['stringData']['admin-password'] == 'private-random-password'
    assert 'private-random-password' not in capsys.readouterr().out


def test_owned_grafana_credentials_are_preserved_on_rerun(tmp_path, monkeypatch):
    op = operator(tmp_path, '--execute')
    current = {'metadata': {'labels': {'app.kubernetes.io/part-of': 'market-intel',
                                       'app.kubernetes.io/managed-by': 'market-intel-provision'}},
               'data': {'admin-user': base64.b64encode(b'admin').decode('ascii'),
                        'admin-password': base64.b64encode(b'not-a-real-password').decode('ascii')}}
    def kubectl(*parts, **kwargs):
        assert parts[2] == 'get'
        return json.dumps(current)
    monkeypatch.setattr(op, 'kubectl_run', kubectl)
    op.ensure_grafana_admin()
