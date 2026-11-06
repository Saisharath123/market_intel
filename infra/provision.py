#!/usr/bin/env python3
"""Reviewable EKS bootstrap. Mutations require --execute; no deletion operations.

Examples (omit --execute to inspect the plan):
  python infra/provision.py foundations --admin-cidr 1.2.3.4/32 --execute
  python infra/provision.py controllers-bootstrap --helm-path /path/to/helm --execute
  python infra/provision.py gitops-staging --execute
  python infra/provision.py gitops-production --execute
  python infra/provision.py harden-cni --execute
  python infra/provision.py status

Only the exact agent profile/principal and dedicated, ownership-tagged stacks are
accepted. Helm and kubeconfig state stay in a sibling runtime directory, outside
Git. Failed operations retain diagnostics; the script never destroys resources.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = '521595303406'
PRINCIPAL = f'arn:aws:iam::{ACCOUNT}:user/Agent'
PROFILE = 'agent'
REGION = 'ap-south-1'
CLUSTER = 'market-intel-prod'
CONTEXT = 'market-intel-sre'
VERSION = '1.36'
TAGS = {'Project': 'market-intel', 'ManagedBy': 'market-intel-provision'}
STACKS = {name: f'market-intel-sre-{name}' for name in ('eks', 'ci', 'controllers', 'routing', 'sonarqube')}
ECR_REPOS = {'dashboard': 'market-intel/dashboard', 'market-api': 'market-intel/market',
             'news-api': 'market-intel/news'}
CHARTS = {
    'argocd': ('argo-cd', 'https://argoproj.github.io/argo-helm', '10.10.5', 'argocd'),
    'argo-rollouts': ('argo-rollouts', 'https://argoproj.github.io/argo-helm', '2.43.8', 'argo-rollouts'),
    'aws-load-balancer-controller': ('aws-load-balancer-controller', 'https://aws.github.io/eks-charts',
                                     '3.6.0', 'kube-system'),
    'monitoring': ('kube-prometheus-stack', 'https://prometheus-community.github.io/helm-charts',
                   '92.11.0', 'monitoring'),
}
SUCCESS = {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE'}
COMPLETE = SUCCESS | {'UPDATE_ROLLBACK_COMPLETE'}


class ProvisionError(RuntimeError):
    """An actionable failure which must not be hidden by a later operation."""


def public_cidr(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use a valid public administrator IPv4 address with /32.') from exc
    if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
        raise argparse.ArgumentTypeError('Administrator access must be one public IPv4 /32, never 0.0.0.0/0.')
    return str(network)


def checked_runtime(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path == ROOT or path.is_relative_to(ROOT):
        raise ProvisionError('Runtime state must be outside the Git checkout. Use its sibling runtime directory.')
    return path


def release_images(path: Path) -> dict[str, str]:
    """Read the deliberately narrow three-service image override file, without a YAML dependency.

    Helm validates full YAML separately. Requiring exactly the expected service
    image keys rejects stale placeholders, duplicate mappings and extra images.
    """
    result: dict[str, str] = {}
    service = None
    in_services = False
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line == 'services:':
            if in_services or result:
                raise ProvisionError(f'Duplicate services mapping in {path.name}.')
            in_services = True
            continue
        if line and not line[0].isspace():
            in_services = False
        if not in_services:
            continue
        match = re.fullmatch(r'  ([a-z][a-z-]*):\s*(?:#.*)?', line)
        if match:
            service = match.group(1)
            continue
        match = re.fullmatch(r'    image:\s*(.*?)\s*(?:#.*)?', line)
        if not match:
            continue
        image = match.group(1).strip().strip('\'"')
        if service not in ECR_REPOS or service in result:
            raise ProvisionError(f'Unexpected or duplicate service image in {path.name}: {service!r}.')
        expected = (f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPOS[service]}'
                    r'@sha256:[0-9a-f]{64}')
        if not re.fullmatch(expected, image):
            raise ProvisionError(f'{path.name}: {service} needs a new approved digest in its dedicated ECR repository.')
        result[service] = image
    if set(result) != set(ECR_REPOS):
        raise ProvisionError(f'{path.name} must contain exactly the three approved service image digests.')
    return result


class Provisioner:
    def __init__(self, args):
        self.args = args
        self.execute = args.execute
        self.runtime = checked_runtime(args.runtime_dir)
        self.kubeconfig = self.runtime / 'kubeconfig'
        self.env = dict(os.environ, AWS_PROFILE=PROFILE, AWS_REGION=REGION,
                        AWS_DEFAULT_REGION=REGION, AWS_PAGER='', KUBECONFIG=str(self.kubeconfig))
        self.helm = args.helm_path
        self.kubectl = args.kubectl_path

    def run(self, command, *, write=False, data=None, timeout=120, quiet=False):
        command = [str(item) for item in command]
        if write and not self.execute:
            print('PLAN ' + shlex.join(command), flush=True)
            return ''
        if not quiet:
            print('$ ' + shlex.join(command), flush=True)
        try:
            proc = subprocess.run(command, input=data, text=True, encoding='utf-8',
                                  capture_output=True, env=self.env, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProvisionError(f'Could not complete {command[0]}: {exc}') from exc
        if proc.returncode:
            detail = (proc.stderr or proc.stdout)[-5000:].strip()
            raise ProvisionError(f'{command[0]} exited {proc.returncode}: {detail}')
        return proc.stdout

    def aws(self, *parts, write=False, timeout=120):
        output = self.run(['aws', *parts, '--profile', PROFILE, '--region', REGION,
                           '--output', 'json', '--no-cli-pager'], write=write, timeout=timeout, quiet=not write)
        if not output.strip():
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProvisionError('AWS returned an unexpected non-JSON response.') from exc

    def identity(self):
        identity = self.aws('sts', 'get-caller-identity')
        if identity.get('Account') != ACCOUNT or identity.get('Arn') != PRINCIPAL:
            raise ProvisionError(f'Refusing this identity. Required profile {PROFILE} and exact principal {PRINCIPAL}.')
        print(f'Identity verified: {PRINCIPAL} in {REGION}', flush=True)

    def describe_stack(self, name):
        try:
            result = self.aws('cloudformation', 'describe-stacks', '--stack-name', name)
        except ProvisionError as exc:
            if 'does not exist' in str(exc) and 'ValidationError' in str(exc):
                return None
            raise
        stack = result['Stacks'][0]
        tags = {item['Key']: item['Value'] for item in stack.get('Tags', [])}
        if any(tags.get(key) != value for key, value in TAGS.items()):
            raise ProvisionError(f'Refusing existing stack {name}: required ownership tags are absent or different.')
        return stack

    @staticmethod
    def outputs(stack):
        return {entry['OutputKey']: entry['OutputValue'] for entry in stack.get('Outputs', [])}

    def wait_stack(self, name, token=None):
        deadline = time.monotonic() + self.args.stack_timeout_minutes * 60
        previous = None
        while time.monotonic() < deadline:
            stack = self.describe_stack(name)
            if not stack:
                raise ProvisionError(f'Stack {name} disappeared while waiting; no cleanup was attempted.')
            status = stack['StackStatus']
            if status != previous:
                print(f'{name}: {status}', flush=True)
                previous = status
            # A post-execute read can briefly see the prior UPDATE_COMPLETE.
            # Match this operation's root-stack event before declaring success.
            if status in SUCCESS:
                if token is None:
                    return stack
                events = self.aws('cloudformation', 'describe-stack-events', '--stack-name', name,
                                  '--max-items', '50').get('StackEvents', [])
                if any(e.get('ClientRequestToken') == token
                       and e.get('ResourceType') == 'AWS::CloudFormation::Stack'
                       and e.get('ResourceStatus') in SUCCESS for e in events):
                    return stack
                time.sleep(5)
                continue
            if not status.endswith('_IN_PROGRESS'):
                events = self.aws('cloudformation', 'describe-stack-events', '--stack-name', name,
                                  '--max-items', '20').get('StackEvents', [])
                failures = [f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}"
                            for e in events if e.get('ResourceStatus', '').endswith('_FAILED')]
                raise ProvisionError(f'Stack stopped in {status}. ' + ' | '.join(failures[:5]))
            time.sleep(15)
        raise ProvisionError(f'Timed out waiting for {name}; inspect its ongoing operation before retrying.')

    def upsert_stack(self, kind, parameters):
        name = STACKS[kind]
        template = ROOT / 'infra' / 'sonarqube.json' if kind == 'sonarqube' else ROOT / 'infra' / 'cloudformation' / f'{kind}.json'
        json.loads(template.read_text(encoding='utf-8-sig'))
        if not self.execute:
            print(f'PLAN create/update ownership-tagged {name} from {template.relative_to(ROOT)}')
            print('PLAN parameters: ' + json.dumps(parameters, sort_keys=True))
            print('PLAN inspect change set; reject removals/replacements unless explicitly permitted; wait for completion.')
            return None
        existing = self.describe_stack(name)
        if existing and existing['StackStatus'].endswith('_IN_PROGRESS') and existing['StackStatus'] != 'REVIEW_IN_PROGRESS':
            existing = self.wait_stack(name)
        if existing and existing['StackStatus'] not in COMPLETE | {'REVIEW_IN_PROGRESS'}:
            raise ProvisionError(f'{name} is {existing["StackStatus"]}; inspect and repair explicitly before retrying.')
        self.aws('cloudformation', 'validate-template', '--template-body', 'file://' + str(template))
        defaults = json.loads(template.read_text())['Parameters']
        current = {p['ParameterKey']: p.get('ParameterValue', '') for p in (existing or {}).get('Parameters', [])}
        for key, definition in defaults.items():
            if key not in parameters:
                if key in current:
                    parameters[key] = current[key]
                elif 'Default' in definition:
                    parameters[key] = definition['Default']
                else:
                    raise ProvisionError(f'Missing required CloudFormation parameter: {key}')
        self.runtime.mkdir(parents=True, exist_ok=True)
        param_path = self.runtime / f'{kind}-parameters.json'
        param_path.write_text(json.dumps([{'ParameterKey': k, 'ParameterValue': str(v)}
                                          for k, v in parameters.items()]), encoding='utf-8')
        change_type = 'CREATE' if not existing or existing['StackStatus'] == 'REVIEW_IN_PROGRESS' else 'UPDATE'
        change_name = 'market-intel-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        stack_tags = {tag['Key']: tag['Value'] for tag in (existing or {}).get('Tags', [])}
        stack_tags.update(TAGS)
        self.aws('cloudformation', 'create-change-set', '--stack-name', name, '--change-set-name', change_name,
                 '--change-set-type', change_type, '--template-body', 'file://' + str(template),
                 '--parameters', 'file://' + str(param_path), '--capabilities', 'CAPABILITY_NAMED_IAM',
                 '--tags', *[f'Key={key},Value={value}' for key, value in stack_tags.items()], write=True)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            change = self.aws('cloudformation', 'describe-change-set', '--stack-name', name,
                              '--change-set-name', change_name)
            if change['Status'] == 'CREATE_COMPLETE':
                break
            if change['Status'] == 'FAILED':
                reason = change.get('StatusReason', '')
                if "didn't contain changes" in reason or 'No updates are to be performed' in reason:
                    print(f'{name}: no changes', flush=True)
                    return self.describe_stack(name)
                raise ProvisionError(f'Change set failed: {reason}')
            time.sleep(5)
        else:
            raise ProvisionError(f'Change set {change_name} did not become ready; it was not executed.')
        risky = []
        for item in change.get('Changes', []):
            resource = item.get('ResourceChange', {})
            print(f"  {resource.get('Action')} {resource.get('LogicalResourceId')} "
                  f"replacement={resource.get('Replacement', 'False')}", flush=True)
            if resource.get('Action') == 'Remove' or resource.get('Replacement') in ('True', 'Conditional'):
                risky.append(resource.get('LogicalResourceId'))
        if risky and not self.args.allow_replacements:
            raise ProvisionError('Change set left unexecuted: removals/replacements require --allow-replacements: '
                                 + ', '.join(risky))
        self.aws('cloudformation', 'execute-change-set', '--stack-name', name,
                 '--change-set-name', change_name, '--client-request-token', change_name,
                 '--disable-rollback', write=True)
        return self.wait_stack(name, token=change_name)

    def existing_provider(self):
        expected = f'arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com'
        providers = self.aws('iam', 'list-open-id-connect-providers')['OpenIDConnectProviderList']
        if not any(item['Arn'] == expected for item in providers):
            return ''
        provider = self.aws('iam', 'get-open-id-connect-provider', '--open-id-connect-provider-arn', expected)
        if provider.get('Url', '').removeprefix('https://').rstrip('/') != 'token.actions.githubusercontent.com':
            raise ProvisionError('Existing GitHub OIDC provider has an unexpected issuer.')
        if 'sts.amazonaws.com' not in provider.get('ClientIDList', []):
            raise ProvisionError('Existing GitHub OIDC provider lacks sts.amazonaws.com audience; it was not changed.')
        return expected

    def preflight_ci_names(self):
        if self.describe_stack(STACKS['ci']):
            return
        for name in ECR_REPOS.values():
            try:
                self.aws('ecr', 'describe-repositories', '--repository-names', name)
            except ProvisionError as exc:
                if 'RepositoryNotFoundException' in str(exc):
                    continue
                raise
            raise ProvisionError(f'ECR repository {name} already exists outside the managed CI stack. Import/review it explicitly.')
        try:
            self.aws('iam', 'get-role', '--role-name', 'market-intel-github-publisher')
        except ProvisionError as exc:
            if 'NoSuchEntity' in str(exc):
                return
            raise
        raise ProvisionError('The named publishing role already exists outside the CI stack; no takeover was attempted.')

    def foundations(self):
        if not self.args.admin_cidr:
            raise ProvisionError('foundations requires --admin-cidr with the approved public IPv4 /32.')
        if self.execute:
            self.identity()
            versions = self.aws('eks', 'describe-cluster-versions', '--cluster-versions', VERSION)['clusterVersions']
            if not any(v.get('clusterVersion') == VERSION and
                       (v.get('versionStatus') or v.get('status')) == 'STANDARD_SUPPORT' for v in versions):
                raise ProvisionError(f'EKS {VERSION} was not confirmed in standard support; review the pinned version.')
            self.preflight_ci_names()
            detected_provider = self.existing_provider()
            ci = self.describe_stack(STACKS['ci'])
            if ci:
                # Preserve whether this stack owns the provider. Switching a
                # created provider to an external ARN would remove its resource.
                provider = next((p['ParameterValue'] for p in ci.get('Parameters', [])
                                 if p['ParameterKey'] == 'ExistingGitHubProviderArn'), '')
                if provider and provider != detected_provider:
                    raise ProvisionError('The CI stack\'s referenced GitHub provider no longer matches the validated issuer.')
                if not detected_provider and ci['StackStatus'] != 'REVIEW_IN_PROGRESS':
                    raise ProvisionError('The CI stack\'s GitHub provider is missing; review before updating.')
            else:
                provider = detected_provider
        else:
            provider = '<detect and validate existing GitHub provider before execution>'
        self.upsert_stack('ci', {'ExistingGitHubProviderArn': provider})
        self.upsert_stack('eks', {'AdminCidr': self.args.admin_cidr,
                                  'KubernetesVersion': VERSION, 'NodeInstanceType': 't3a.large'})

    def kubectl_run(self, *parts, write=False, data=None, timeout=120):
        return self.run([self.kubectl, '--kubeconfig', self.kubeconfig, '--context', CONTEXT, *parts],
                        write=write, data=data, timeout=timeout)

    def sonarqube(self):
        if not self.execute:
            print('PLAN create private SonarQube Community Build and PostgreSQL on one dedicated t3a.large host.')
            print('PLAN reuse the owned EKS VPC/private subnet/NAT, encrypted 60GiB volume; no inbound ports.')
            print('PLAN GitHub main-only OIDC grants a fixed-port SSM tunnel, without EKS administration.')
            return
        self.identity()
        stack = self.describe_stack(STACKS['eks'])
        if not stack or stack['StackStatus'] not in SUCCESS:
            raise ProvisionError('The EKS foundation must complete before deploying private SonarQube.')
        outputs = self.outputs(stack)
        provider = self.existing_provider()
        if not provider:
            raise ProvisionError('The validated GitHub OIDC provider is required for Sonar CI access.')
        result = self.upsert_stack('sonarqube', {'VpcId': outputs['VpcId'],
                                                'SubnetId': outputs['PrivateSubnets'].split(',')[0],
                                                'GitHubProviderArn': provider})
        print(json.dumps(self.outputs(result), indent=2), flush=True)

    def connect(self):
        if not self.execute:
            print(f'PLAN use isolated kubeconfig {self.kubeconfig}, context {CONTEXT}.')
            return {'VpcId': '<owned VPC output>'}
        self.identity()
        stack = self.describe_stack(STACKS['eks'])
        if not stack or stack['StackStatus'] not in COMPLETE:
            raise ProvisionError('The dedicated EKS stack must complete before Kubernetes bootstrap.')
        cluster = self.aws('eks', 'describe-cluster', '--name', CLUSTER)['cluster']
        if cluster.get('arn') != f'arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}' or cluster.get('tags', {}).get('Project') != 'market-intel':
            raise ProvisionError('Cluster ARN or Project ownership does not match the dedicated environment.')
        if cluster.get('status') != 'ACTIVE':
            raise ProvisionError('Cluster is not ACTIVE.')
        self.runtime.mkdir(parents=True, exist_ok=True)
        output = self.run(['aws', 'eks', 'update-kubeconfig', '--name', CLUSTER, '--region', REGION,
                           '--profile', PROFILE, '--alias', CONTEXT, '--kubeconfig', self.kubeconfig], write=True)
        if output.strip():
            print('Isolated kubeconfig updated.', flush=True)
        try:
            self.kubeconfig.chmod(0o600)
        except OSError:
            pass
        version = json.loads(self.kubectl_run('version', '--client', '-o', 'json'))['clientVersion']
        minor = int(re.sub(r'\D.*', '', version['minor']))
        if version['major'] != '1' or not 35 <= minor <= 37:
            raise ProvisionError('Use kubectl 1.36.x (supported client skew: 1.35 through 1.37). Pass --kubectl-path.')
        self.kubectl_run('wait', '--for=condition=Ready', 'nodes', '--all', '--timeout=10m', timeout=660)
        return self.outputs(stack)

    def wait_addon(self, name):
        if not self.execute:
            print(f'PLAN require ACTIVE EKS add-on {name}.')
            return
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            addon = self.aws('eks', 'describe-addon', '--cluster-name', CLUSTER, '--addon-name', name)['addon']
            if addon['status'] == 'ACTIVE':
                return addon
            if addon['status'] in ('CREATE_FAILED', 'UPDATE_FAILED', 'DELETE_FAILED', 'DEGRADED'):
                raise ProvisionError(f'Add-on {name} is {addon["status"]}: {addon.get("health", {})}')
            time.sleep(15)
        raise ProvisionError(f'Add-on {name} did not become ACTIVE.')

    def install_chart(self, release, *, values=None, settings=()):
        chart, repository, version, namespace = CHARTS[release]
        if self.execute:
            existing = json.loads(self.run([self.helm, 'list', '-n', namespace, '--filter', '^' + release + '$', '-o', 'json',
                                            '--kubeconfig', self.kubeconfig, '--kube-context', CONTEXT]))
            if existing and not existing[0].get('chart', '').startswith(chart + '-'):
                raise ProvisionError(f'Refusing to replace unrelated Helm release {release}.')
        command = [self.helm, 'upgrade', '--install', release, chart, '--repo', repository, '--version', version,
                   '--namespace', namespace, '--create-namespace', '--kubeconfig', self.kubeconfig,
                   '--kube-context', CONTEXT, '--repository-cache', self.runtime / 'helm-cache',
                   '--repository-config', self.runtime / 'helm-repositories.yaml',
                   '--registry-config', self.runtime / 'helm-registry.json', '--wait', '--timeout', '20m',
                   '--history-max', '5']
        if values:
            command += ['--values', ROOT / values]
        for setting in settings:
            # Helm string "false" is truthy in templates. Preserve actual
            # boolean types for the enable/create switches used here.
            flag = '--set' if setting.rsplit('=', 1)[-1] in ('true', 'false') else '--set-string'
            command += [flag, setting]
        self.run(command, write=True, timeout=1320)

    def namespace(self, name, *, restricted=False):
        labels = {'app.kubernetes.io/part-of': 'market-intel'}
        if name == 'market-intel':
            # Admission injects ALB target readiness gates only for Pods created
            # after the namespace has this label and eligible targets exist.
            labels['elbv2.k8s.aws/pod-readiness-gate-inject'] = 'enabled'
        if restricted:
            labels.update({'pod-security.kubernetes.io/enforce': 'restricted',
                           'pod-security.kubernetes.io/enforce-version': 'v' + VERSION,
                           'pod-security.kubernetes.io/audit': 'restricted', 'pod-security.kubernetes.io/warn': 'restricted'})
        document = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name, 'labels': labels}}
        self.kubectl_run('apply', '-f', '-', write=True, data=json.dumps(document))

    def ensure_grafana_admin(self):
        """Generate once in-cluster; never expose credentials in argv, logs or files."""
        name = 'market-intel-grafana-admin'
        if not self.execute:
            print(f'PLAN create/reuse strong Grafana admin Secret monitoring/{name}; credentials are never logged.')
            return
        raw = self.kubectl_run('-n', 'monitoring', 'get', 'secret', name, '-o', 'json', '--ignore-not-found')
        ownership = {'app.kubernetes.io/part-of': 'market-intel',
                     'app.kubernetes.io/managed-by': 'market-intel-provision'}
        if raw.strip():
            current = json.loads(raw)
            labels = current.get('metadata', {}).get('labels', {})
            if any(labels.get(key) != value for key, value in ownership.items()):
                raise ProvisionError('Refusing to reuse the Grafana administrator Secret: ownership labels do not match.')
            if not all(current.get('data', {}).get(key) for key in ('admin-user', 'admin-password')):
                raise ProvisionError('The owned Grafana administrator Secret is missing its required keys.')
            print('Owned Grafana administrator Secret already exists; preserving its credentials.', flush=True)
            return
        document = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                    'metadata': {'name': name, 'namespace': 'monitoring', 'labels': ownership},
                    'stringData': {'admin-user': 'admin', 'admin-password': secrets.token_urlsafe(32)}}
        self.kubectl_run('create', '-f', '-', write=True, data=json.dumps(document))

    def controllers_bootstrap(self):
        outputs = self.connect()
        self.wait_addon('eks-pod-identity-agent')
        self.wait_addon('vpc-cni')
        self.upsert_stack('controllers', {'ClusterName': CLUSTER})
        self.wait_addon('aws-ebs-csi-driver')
        self.install_chart('aws-load-balancer-controller', settings=(
            f'clusterName={CLUSTER}', f'region={REGION}', f'vpcId={outputs["VpcId"]}',
            'serviceAccount.create=true', 'serviceAccount.name=aws-load-balancer-controller'))
        self.install_chart('argocd', values='deploy/argocd/values.yaml')
        self.namespace('market-intel', restricted=True)
        self.namespace('market-intel-staging', restricted=True)
        self.namespace('monitoring')
        self.kubectl_run('apply', '-f', ROOT / 'deploy/monitoring/storage-class.yaml', write=True)
        self.ensure_grafana_admin()
        self.install_chart('monitoring', values='deploy/monitoring/values.yaml')
        # Rollouts' pinned values create a ServiceMonitor; the CRD must exist
        # before Helm submits that custom resource to the API server.
        self.kubectl_run('wait', '--for=condition=Established',
                         'crd/servicemonitors.monitoring.coreos.com', 'crd/prometheusrules.monitoring.coreos.com',
                         '--timeout=2m', write=not self.execute, timeout=180)
        self.install_chart('argo-rollouts', values='deploy/rollouts/controller-values.yaml')
        self.kubectl_run('apply', '-f', ROOT / 'deploy/monitoring/api-dashboard.yaml', write=True)
        print('Bootstrap complete/planned. Application rollout requires approved Git image digests.', flush=True)

    def verify_git_release(self):
        dirty = self.run(['git', '-C', ROOT, 'status', '--porcelain'], quiet=True)
        if dirty.strip():
            raise ProvisionError('Commit and push the reviewed revision before GitOps bootstrap; the checkout is dirty.')
        local = self.run(['git', '-C', ROOT, 'rev-parse', 'HEAD'], quiet=True).strip()
        remote = self.run(['git', '-C', ROOT, 'ls-remote', 'origin', 'refs/heads/main'], quiet=True).split()
        if not remote or remote[0] != local:
            raise ProvisionError('Local HEAD must equal pushed origin/main before applying its Argo Application.')
        return local

    def verify_ecr_images(self, images):
        for service, reference in images.items():
            digest = reference.split('@', 1)[1]
            response = self.aws('ecr', 'batch-get-image', '--repository-name', ECR_REPOS[service],
                                '--image-ids', 'imageDigest=' + digest)
            if response.get('failures') or len(response.get('images', [])) != 1:
                raise ProvisionError(f'Approved image does not exist in ECR: {service} {digest}.')

    def gitops(self, production=False):
        environment = 'production' if production else 'staging'
        app = 'market-intel' if production else 'market-intel-staging'
        path = ROOT / 'deploy/chart' / f'values-{environment}.yaml'
        if not self.execute:
            try:
                release_images(path)
            except ProvisionError as exc:
                print(f'PLAN BLOCKER: {exc}')
            print('PLAN require clean, pushed origin/main; verify all three ECR digests exist.')
            if production:
                print('PLAN require healthy/synced successful staging at this revision, using identical digests.')
            self.connect()
        else:
            revision = self.verify_git_release()
            images = release_images(path)
            self.connect()
            self.verify_ecr_images(images)
            if production:
                if images != release_images(ROOT / 'deploy/chart/values-staging.yaml'):
                    raise ProvisionError('Production must promote exactly the staged digests without rebuilding.')
                staging = json.loads(self.kubectl_run('-n', 'argocd', 'get', 'application', 'market-intel-staging', '-o', 'json'))
                status = staging.get('status', {})
                if not (status.get('sync', {}).get('status') == 'Synced'
                        and status.get('sync', {}).get('revision') == revision
                        and status.get('health', {}).get('status') == 'Healthy'
                        and status.get('operationState', {}).get('phase') == 'Succeeded'):
                    raise ProvisionError('Staging must be Synced/Healthy with successful hooks at the promoted revision.')
        self.kubectl_run('apply', '-f', ROOT / 'deploy/argocd/project.yaml', write=True)
        application = 'application.yaml' if production else 'application-staging.yaml'
        self.kubectl_run('apply', '-f', ROOT / 'deploy/argocd' / application, write=True)
        if production:
            self.configure_dashboard_route()
        if self.execute:
            for field, expected in (('sync.revision', revision), ('sync.status', 'Synced'),
                                    ('health.status', 'Healthy'), ('operationState.phase', 'Succeeded')):
                self.kubectl_run('-n', 'argocd', 'wait', f'application/{app}',
                                 f'--for=jsonpath={{.status.{field}}}={expected}', '--timeout=25m', timeout=1560)
            print(f'{app}: desired revision {revision}, Synced/Healthy, hook operation Succeeded.', flush=True)

    def configure_dashboard_route(self):
        """Publish the owned internal ALB under the fixed private API name.

        Apply the Argo application first: Rollout/Ingress creation does not depend
        on dashboard connectivity. Resolve DNS before waiting for PostSync smoke.
        """
        if not self.execute:
            print('PLAN wait for production internal ALB and configure owned private api.market-intel.internal DNS.')
            return
        stack = self.describe_stack(STACKS['eks'])
        outputs = self.outputs(stack)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            raw = self.kubectl_run('-n', 'market-intel', 'get', 'ingress', 'market-api-canary',
                                   '-o', 'json', '--ignore-not-found')
            ingress = json.loads(raw) if raw.strip() else {}
            addresses = ingress.get('status', {}).get('loadBalancer', {}).get('ingress', [])
            hostname = next((item.get('hostname', '') for item in addresses if item.get('hostname')), '')
            if hostname:
                break
            time.sleep(10)
        else:
            raise ProvisionError('Production ALB did not receive a hostname; inspect Argo/Ingress before retrying.')
        if not re.fullmatch(r'internal-[a-z0-9-]+\.ap-south-1\.elb\.amazonaws\.com', hostname):
            raise ProvisionError('Refusing a non-internal or unexpected-region ALB hostname.')
        balancers = self.aws('elbv2', 'describe-load-balancers')['LoadBalancers']
        matches = [lb for lb in balancers if lb.get('DNSName') == hostname]
        if len(matches) != 1 or matches[0].get('VpcId') != outputs['VpcId'] or matches[0].get('Scheme') != 'internal':
            raise ProvisionError('Production ALB must be internal and belong to the owned EKS VPC.')
        self.upsert_stack('routing', {'HostedZoneId': outputs['PrivateApiZoneId'], 'AlbDnsName': hostname})

    def harden_cni(self):
        if not self.execute:
            print('PLAN verify active Pod Identity agent and VPC CNI add-ons, exact aws-node role association, and aws-node rollout.')
            self.upsert_stack('eks', {'BootstrapNodeCniPolicy': 'false'})
            return
        self.connect()
        self.wait_addon('eks-pod-identity-agent')
        self.wait_addon('vpc-cni')
        stack = self.describe_stack(STACKS['eks'])
        expected_role = self.outputs(stack)['CniRoleArn']
        associations = self.aws('eks', 'list-pod-identity-associations', '--cluster-name', CLUSTER,
                                '--namespace', 'kube-system', '--service-account', 'aws-node')['associations']
        if len(associations) != 1:
            raise ProvisionError('Expected exactly one dedicated aws-node Pod Identity association.')
        association = self.aws('eks', 'describe-pod-identity-association', '--cluster-name', CLUSTER,
                               '--association-id', associations[0]['associationId'])['association']
        if association.get('roleArn') != expected_role:
            raise ProvisionError('aws-node Pod Identity association does not use this stack\'s CNI role.')
        self.kubectl_run('-n', 'kube-system', 'rollout', 'status', 'daemonset/aws-node', '--timeout=5m', timeout=360)
        pods = json.loads(self.kubectl_run('-n', 'kube-system', 'get', 'pods', '-l', 'k8s-app=aws-node', '-o', 'json'))
        if not pods.get('items'):
            raise ProvisionError('No aws-node Pods were found.')
        for pod in pods['items']:
            container = next((c for c in pod['spec']['containers'] if c['name'] == 'aws-node'), None)
            names = {item['name'] for item in (container or {}).get('env', [])}
            if not {'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE'} <= names:
                raise ProvisionError('An aws-node Pod lacks the injected Pod Identity credential environment.')
        self.upsert_stack('eks', {'BootstrapNodeCniPolicy': 'false'})
        self.kubectl_run('-n', 'kube-system', 'rollout', 'status', 'daemonset/aws-node', '--timeout=5m', timeout=360)
        print('Node CNI policy removed through CloudFormation; no manual IAM drift introduced.', flush=True)

    def status(self):
        self.identity()
        for kind, name in STACKS.items():
            stack = self.describe_stack(name)
            print(json.dumps({'stack': name, 'status': stack['StackStatus'] if stack else 'ABSENT',
                              'outputs': self.outputs(stack) if stack else {}}, indent=2), flush=True)
        if self.kubeconfig.exists() and (Path(self.kubectl).is_file() or shutil.which(self.kubectl)):
            print(self.kubectl_run('-n', 'argocd', 'get', 'applications'), end='', flush=True)
            for namespace in ('market-intel-staging', 'market-intel', 'monitoring'):
                print(self.kubectl_run('-n', namespace, 'get', 'pods'), end='', flush=True)


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument('phase', choices=('foundations', 'sonarqube', 'controllers-bootstrap', 'gitops-staging',
                                         'gitops-production', 'harden-cni', 'status'))
    result.add_argument('--execute', action='store_true', help='Execute reviewed mutations; otherwise print a plan. status is read-only.')
    result.add_argument('--admin-cidr', type=public_cidr)
    result.add_argument('--helm-path', default='helm')
    result.add_argument('--kubectl-path', default='kubectl')
    result.add_argument('--runtime-dir', type=Path, default=ROOT.parent / 'market-intel-runtime')
    result.add_argument('--stack-timeout-minutes', type=int, default=120)
    result.add_argument('--allow-replacements', action='store_true',
                        help='Explicitly permit change-set resource removals/replacements after reviewing the printed changes.')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.stack_timeout_minutes <= 0:
        raise ProvisionError('Stack timeout must be positive.')
    operator = Provisioner(args)
    if args.phase == 'gitops-staging':
        operator.gitops()
    elif args.phase == 'gitops-production':
        operator.gitops(production=True)
    else:
        getattr(operator, args.phase.replace('-', '_'))()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ProvisionError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
