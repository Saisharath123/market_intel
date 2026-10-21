"""Apply the reviewed INTERNAL-ACCESS-PLAN.md from authenticated AWS CloudShell.

Run phases separately: controllers, vpn, monitoring, ingress, dns.
Private keys remain outside Git and are never printed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

REGION = 'ap-south-1'
ACCOUNT = '521595303406'
CLUSTER = 'market-intel-prod'
ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path.home() / '.market-intel-private'
PRIVATE.mkdir(mode=0o700, exist_ok=True)
STATE = PRIVATE / 'state.json'
os.umask(0o077)
os.environ['AWS_DEFAULT_REGION'] = REGION
os.environ['AWS_PAGER'] = ''
state = json.loads(STATE.read_text()) if STATE.exists() else {}


def run(args, data=None):
    proc = subprocess.run(args, input=data, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed: {proc.stderr[-2000:]}')
    return proc.stdout


def aws(*args):
    output = run(['aws', *args, '--output', 'json'])
    return json.loads(output) if output.strip() else {}


def save():
    STATE.write_text(json.dumps(state, indent=2))


def apply(obj):
    return run(['kubectl', 'apply', '-f', '-'], json.dumps(obj))


def tagged_policy(name, document):
    arn = f'arn:aws:iam::{ACCOUNT}:policy/{name}'
    policies = aws('iam', 'list-policies', '--scope', 'Local')['Policies']
    if not any(p['Arn'] == arn for p in policies):
        aws('iam', 'create-policy', '--policy-name', name, '--policy-document', 'file://' + str(document),
            '--tags', 'Key=Project,Value=market-intel')
    return arn


def irsa(name, namespace, sa, policy):
    issuer = state['issuer'].replace('https://', '')
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
             'Principal': {'Federated': f'arn:aws:iam::{ACCOUNT}:oidc-provider/{issuer}'},
             'Action': 'sts:AssumeRoleWithWebIdentity', 'Condition': {'StringEquals': {
                 issuer + ':aud': 'sts.amazonaws.com', issuer + ':sub': f'system:serviceaccount:{namespace}:{sa}'}}}]}
    roles = aws('iam', 'list-roles')['Roles']
    if not any(r['RoleName'] == name for r in roles):
        aws('iam', 'create-role', '--role-name', name, '--assume-role-policy-document', json.dumps(trust),
            '--tags', 'Key=Project,Value=market-intel')
    else:
        tags = aws('iam', 'list-role-tags', '--role-name', name)['Tags']
        assert {'Key': 'Project', 'Value': 'market-intel'} in tags, 'Existing role ownership mismatch'
    aws('iam', 'attach-role-policy', '--role-name', name, '--policy-arn', policy)
    arn = f'arn:aws:iam::{ACCOUNT}:role/{name}'
    apply({'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': sa, 'namespace': namespace,
           'annotations': {'eks.amazonaws.com/role-arn': arn}}})
    return arn


def controllers():
    policy = tagged_policy('market-intel-load-balancer-controller', ROOT / 'infra/aws-load-balancer-controller-policy.json')
    irsa('market-intel-load-balancer-controller', 'kube-system', 'aws-load-balancer-controller', policy)
    ebs = irsa('market-intel-ebs-csi', 'kube-system', 'ebs-csi-controller-sa',
               'arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy')
    addons = aws('eks', 'list-addons', '--cluster-name', CLUSTER)['addons']
    if 'aws-ebs-csi-driver' not in addons:
        aws('eks', 'create-addon', '--cluster-name', CLUSTER, '--addon-name', 'aws-ebs-csi-driver',
            '--service-account-role-arn', ebs)
    run(['helm', 'repo', 'add', 'eks', 'https://aws.github.io/eks-charts', '--force-update'])
    run(['helm', 'repo', 'update', 'eks'])
    run(['helm', 'upgrade', '--install', 'aws-load-balancer-controller', 'eks/aws-load-balancer-controller',
         '--version', '3.6.0', '-n', 'kube-system', '--set', 'clusterName=' + CLUSTER,
         '--set', 'region=' + REGION, '--set', 'vpcId=' + state['vpc'],
         '--set', 'serviceAccount.create=false', '--set', 'serviceAccount.name=aws-load-balancer-controller',
         '--wait', '--timeout', '10m'])
    aws('iam', 'put-role-policy', '--role-name', 'market-intel-image-publisher',
        '--policy-name', 'market-intel-ecr-publish', '--policy-document', 'file://' + str(ROOT / 'infra/github-image-permissions.json'))
    print('Dedicated controllers and scan-read permission configured')


def openssl(*args):
    return run(['openssl', *args])


def ca(name):
    key, cert = PRIVATE / (name + '.key'), PRIVATE / (name + '.crt')
    if not key.exists():
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '3650',
                '-keyout', str(key), '-out', str(cert), '-subj', '/CN=Market Intel ' + name,
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    secret_name = 'market-intel/' + name
    secrets = aws('secretsmanager', 'list-secrets')['SecretList']
    if not any(s['Name'] == secret_name for s in secrets):
        payload = PRIVATE / (name + '.secret.json')
        payload.write_text(json.dumps({'key': key.read_text(), 'certificate': cert.read_text()}))
        aws('secretsmanager', 'create-secret', '--name', secret_name, '--secret-string', 'file://' + str(payload),
            '--tags', 'Key=Project,Value=market-intel')
        payload.unlink()
    return key, cert


def certificate(name, ca_pair, san, client=False):
    key, cert = PRIVATE / (name + '.key'), PRIVATE / (name + '.crt')
    if not cert.exists():
        csr, ext = PRIVATE / (name + '.csr'), PRIVATE / (name + '.ext')
        openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(csr),
                '-subj', '/CN=' + name)
        ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                       'extendedKeyUsage=' + ('clientAuth' if client else 'serverAuth') + '\nsubjectAltName=' + san + '\n')
        openssl('x509', '-req', '-in', str(csr), '-CA', str(ca_pair[1]), '-CAkey', str(ca_pair[0]),
                '-CAcreateserial', '-out', str(cert), '-days', '90' if client else '365', '-sha256', '-extfile', str(ext))
    return key, cert


def import_cert(label, pair, issuer):
    if label not in state:
        state[label] = aws('acm', 'import-certificate', '--certificate', 'fileb://' + str(pair[1]),
                           '--private-key', 'fileb://' + str(pair[0]), '--certificate-chain', 'fileb://' + str(issuer[1]),
                           '--tags', 'Key=Project,Value=market-intel')['CertificateArn']
        save()
    return state[label]


def group(name, description):
    groups = aws('ec2', 'describe-security-groups', '--filters', 'Name=vpc-id,Values=' + state['vpc'],
                 'Name=group-name,Values=' + name)['SecurityGroups']
    if groups:
        return groups[0]['GroupId']
    return aws('ec2', 'create-security-group', '--group-name', name, '--description', description,
               '--vpc-id', state['vpc'], '--tag-specifications',
               'ResourceType=security-group,Tags=[{Key=Project,Value=market-intel}]')['GroupId']


def vpn():
    vpn_ca = ca('vpn-ca')
    https_ca = ca('https-ca')
    server = certificate('vpn.market-intel.internal', vpn_ca, 'DNS:vpn.market-intel.internal')
    client = certificate('saisharath-primary', vpn_ca, 'DNS:saisharath-primary', client=True)
    tls = certificate('dashboard.market-intel.internal', https_ca,
                      'DNS:dashboard.market-intel.internal,DNS:argocd.market-intel.internal,DNS:grafana.market-intel.internal')
    vpn_cert = import_cert('vpn_certificate', server, vpn_ca)
    import_cert('https_certificate', tls, https_ca)
    state['vpn_sg'] = group('market-intel-vpn', 'Project VPN association security group')
    state['alb_sg'] = group('market-intel-internal-https', 'HTTPS from project VPN only')
    rules = aws('ec2', 'describe-security-group-rules', '--filters', 'Name=group-id,Values=' + state['alb_sg'])['SecurityGroupRules']
    if not any(r.get('ReferencedGroupInfo', {}).get('GroupId') == state['vpn_sg'] and r.get('FromPort') == 443 for r in rules):
        aws('ec2', 'authorize-security-group-ingress', '--group-id', state['alb_sg'], '--ip-permissions',
            json.dumps([{'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443,
                         'UserIdGroupPairs': [{'GroupId': state['vpn_sg']}]}]))
    save()
    log_group = '/market-intel/client-vpn'
    groups = aws('logs', 'describe-log-groups', '--log-group-name-prefix', log_group)['logGroups']
    if not any(g['logGroupName'] == log_group for g in groups):
        aws('logs', 'create-log-group', '--log-group-name', log_group)
    aws('logs', 'put-retention-policy', '--log-group-name', log_group, '--retention-in-days', '30')
    if 'vpn_endpoint' not in state:
        result = aws('ec2', 'create-client-vpn-endpoint', '--client-cidr-block', '10.90.0.0/22',
                     '--server-certificate-arn', vpn_cert, '--authentication-options',
                     json.dumps([{'Type': 'certificate-authentication', 'MutualAuthentication': {'ClientRootCertificateChainArn': vpn_cert}}]),
                     '--connection-log-options', json.dumps({'Enabled': True, 'CloudwatchLogGroup': log_group}),
                     '--transport-protocol', 'udp', '--vpn-port', '443', '--split-tunnel',
                     '--dns-servers', '10.80.0.2', '--vpc-id', state['vpc'], '--security-group-ids', state['vpn_sg'],
                     '--session-timeout-hours', '8', '--client-token', 'market-intel-internal-vpn-v1',
                     '--tag-specifications', 'ResourceType=client-vpn-endpoint,Tags=[{Key=Project,Value=market-intel},{Key=Name,Value=market-intel-internal}]')
        state['vpn_endpoint'] = result['ClientVpnEndpointId']
        save()
    endpoint = state['vpn_endpoint']
    associations = aws('ec2', 'describe-client-vpn-target-networks', '--client-vpn-endpoint-id', endpoint)['ClientVpnTargetNetworks']
    for subnet in state['private_subnets']:
        if not any(a['TargetNetworkId'] == subnet and a['Status']['Code'] != 'disassociated' for a in associations):
            aws('ec2', 'associate-client-vpn-target-network', '--client-vpn-endpoint-id', endpoint, '--subnet-id', subnet)
    rules = aws('ec2', 'describe-client-vpn-authorization-rules', '--client-vpn-endpoint-id', endpoint)['AuthorizationRules']
    if not any(r['DestinationCidr'] == '10.80.0.0/16' for r in rules):
        aws('ec2', 'authorize-client-vpn-ingress', '--client-vpn-endpoint-id', endpoint,
            '--target-network-cidr', '10.80.0.0/16', '--authorize-all-groups', '--description', 'Project VPC only')
    config = aws('ec2', 'export-client-vpn-client-configuration', '--client-vpn-endpoint-id', endpoint)['ClientConfiguration']
    profile = PRIVATE / 'market-intel-private.ovpn'
    profile.write_text(config + '\n<cert>\n' + client[1].read_text() + '</cert>\n<key>\n' + client[0].read_text() + '</key>\n')
    print('VPN endpoint:', endpoint)
    print('Private profile prepared in ~/.market-intel-private/market-intel-private.ovpn; operator download only')
    print('HTTPS trust certificate: ~/.market-intel-private/https-ca.crt')


def monitoring():
    run(['aws', 'eks', 'wait', 'addon-active', '--cluster-name', CLUSTER, '--addon-name', 'aws-ebs-csi-driver'])
    run(['kubectl', 'apply', '-f', str(ROOT / 'deploy/monitoring/storage-class.yaml')])
    run(['helm', 'repo', 'add', 'prometheus-community', 'https://prometheus-community.github.io/helm-charts', '--force-update'])
    run(['helm', 'repo', 'update', 'prometheus-community'])
    run(['helm', 'upgrade', '--install', 'monitoring', 'prometheus-community/kube-prometheus-stack',
         '--version', '92.11.0', '-n', 'monitoring', '--create-namespace',
         '-f', str(ROOT / 'deploy/monitoring/values.yaml'), '--wait', '--timeout', '15m'])
    run(['kubectl', 'apply', '-f', str(ROOT / 'deploy/monitoring/api-dashboard.yaml')])
    print('Monitoring installed; verify PVCs, targets and dashboards')


def ingress():
    for namespace, service, host, port, health in [
        ('market-intel', 'dashboard', 'dashboard', 8501, '/_stcore/health'),
        ('argocd', 'argocd-server', 'argocd', 443, '/healthz'),
        ('monitoring', 'monitoring-grafana', 'grafana', 80, '/api/health'),
    ]:
        annotations = {'alb.ingress.kubernetes.io/scheme': 'internal', 'alb.ingress.kubernetes.io/target-type': 'ip',
                       'alb.ingress.kubernetes.io/group.name': 'market-intel-private',
                       'alb.ingress.kubernetes.io/load-balancer-name': 'market-intel-private',
                       'alb.ingress.kubernetes.io/listen-ports': '[{"HTTPS":443}]',
                       'alb.ingress.kubernetes.io/certificate-arn': state['https_certificate'],
                       'alb.ingress.kubernetes.io/security-groups': state['alb_sg'],
                       'alb.ingress.kubernetes.io/manage-backend-security-group-rules': 'true',
                       'alb.ingress.kubernetes.io/subnets': ','.join(state['private_subnets']),
                       'alb.ingress.kubernetes.io/ssl-policy': 'ELBSecurityPolicy-TLS13-1-2-2021-06',
                       'alb.ingress.kubernetes.io/healthcheck-path': health,
                       'alb.ingress.kubernetes.io/backend-protocol': 'HTTPS' if host == 'argocd' else 'HTTP',
                       'alb.ingress.kubernetes.io/load-balancer-attributes': 'idle_timeout.timeout_seconds=3600',
                       'alb.ingress.kubernetes.io/target-group-attributes': 'stickiness.enabled=true,stickiness.lb_cookie.duration_seconds=3600'}
        apply({'apiVersion': 'networking.k8s.io/v1', 'kind': 'Ingress',
               'metadata': {'name': 'private-https', 'namespace': namespace, 'annotations': annotations},
               'spec': {'ingressClassName': 'alb', 'rules': [{'host': host + '.market-intel.internal', 'http': {'paths': [
                   {'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': service, 'port': {'number': port}}}}]}}]}})
    print('Internal HTTPS ingress requested; no public listener')


def dns():
    balancers = aws('elbv2', 'describe-load-balancers', '--names', 'market-intel-private')['LoadBalancers']
    alb = balancers[0]
    assert alb['Scheme'] == 'internal'
    zones = aws('route53', 'list-hosted-zones-by-name', '--dns-name', 'market-intel.internal')['HostedZones']
    zones = [z for z in zones if z['Name'] == 'market-intel.internal.' and z['Config']['PrivateZone']]
    if not zones:
        zone = aws('route53', 'create-hosted-zone', '--name', 'market-intel.internal', '--caller-reference',
                   'market-intel-internal-' + state['vpc'], '--vpc', 'VPCRegion=' + REGION + ',VPCId=' + state['vpc'],
                   '--hosted-zone-config', 'PrivateZone=true')['HostedZone']
    else:
        zone = zones[0]
    changes = [{'Action': 'UPSERT', 'ResourceRecordSet': {'Name': name + '.market-intel.internal', 'Type': 'A',
                'AliasTarget': {'HostedZoneId': alb['CanonicalHostedZoneId'], 'DNSName': alb['DNSName'], 'EvaluateTargetHealth': True}}}
               for name in ('dashboard', 'argocd', 'grafana')]
    aws('route53', 'change-resource-record-sets', '--hosted-zone-id', zone['Id'], '--change-batch', json.dumps({'Changes': changes}))
    state['alb_dns'] = alb['DNSName']
    state['zone'] = zone['Id']
    save()
    print('Private HTTPS DNS configured for dashboard, argocd and grafana')


if __name__ == '__main__':
    assert aws('sts', 'get-caller-identity')['Account'] == ACCOUNT
    cluster = aws('eks', 'describe-cluster', '--name', CLUSTER)['cluster']
    state['vpc'] = cluster['resourcesVpcConfig']['vpcId']
    state['issuer'] = cluster['identity']['oidc']['issuer']
    subnets = aws('ec2', 'describe-subnets', '--filters', 'Name=vpc-id,Values=' + state['vpc'])['Subnets']
    private = [s for s in subnets if any(t['Key'] == 'kubernetes.io/role/internal-elb' for t in s.get('Tags', []))]
    state['private_subnets'] = [s['SubnetId'] for s in sorted(private, key=lambda s: s['AvailabilityZone'])]
    assert len(state['private_subnets']) == 2, 'Expected exactly two project private subnets'
    save()
    {'controllers': controllers, 'vpn': vpn, 'monitoring': monitoring, 'ingress': ingress, 'dns': dns}[sys.argv[1]]()
