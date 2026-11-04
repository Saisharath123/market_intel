"""Read-only inventory of common billed services across enabled AWS regions."""
import concurrent.futures
import json
from pathlib import Path

import boto3
from botocore.config import Config

CONFIG = Config(connect_timeout=4, read_timeout=10, retries={'max_attempts': 1})
session = boto3.Session()
account = session.client('sts', config=CONFIG).get_caller_identity()['Account']
assert account == '521595303406'
regions = [r['RegionName'] for r in session.client('ec2', region_name='ap-south-1', config=CONFIG).describe_regions()['Regions']]


def scan(region):
    result, errors = {}, []

    def collect(service, operation, key, fields, label=None, **kwargs):
        try:
            client = session.client(service, region_name=region, config=CONFIG)
            pages = client.get_paginator(operation).paginate(**kwargs) if client.can_paginate(operation) else [getattr(client, operation)(**kwargs)]
            rows = []
            for page in pages:
                items = page[key]
                if key == 'Reservations':
                    items = [i for reservation in items for i in reservation['Instances']]
                rows.extend({f: item.get(f) for f in fields} if isinstance(item, dict) else item for item in items)
            if rows:
                result[label or service + ':' + operation] = rows
        except Exception as exc:
            errors.append(service + ':' + operation + ':' + type(exc).__name__)

    collect('ec2', 'describe_instances', 'Reservations', ['InstanceId', 'State', 'VpcId', 'Tags'])
    collect('ec2', 'describe_volumes', 'Volumes', ['VolumeId', 'State', 'Size', 'Attachments', 'Tags'])
    collect('ec2', 'describe_snapshots', 'Snapshots', ['SnapshotId', 'VolumeId', 'State', 'VolumeSize'], OwnerIds=[account])
    collect('ec2', 'describe_nat_gateways', 'NatGateways', ['NatGatewayId', 'State', 'VpcId', 'NatGatewayAddresses'])
    collect('ec2', 'describe_addresses', 'Addresses', ['AllocationId', 'AssociationId', 'Tags'])
    collect('ec2', 'describe_vpc_endpoints', 'VpcEndpoints', ['VpcEndpointId', 'VpcEndpointType', 'State', 'VpcId'])
    collect('ec2', 'describe_client_vpn_endpoints', 'ClientVpnEndpoints', ['ClientVpnEndpointId', 'Status', 'Tags'])
    collect('ec2', 'describe_reserved_instances', 'ReservedInstances', ['ReservedInstancesId', 'State', 'End'])
    collect('elbv2', 'describe_load_balancers', 'LoadBalancers', ['LoadBalancerArn', 'LoadBalancerName', 'VpcId', 'State'])
    collect('elb', 'describe_load_balancers', 'LoadBalancerDescriptions', ['LoadBalancerName', 'VPCId'])
    collect('eks', 'list_clusters', 'clusters', [])
    collect('rds', 'describe_db_instances', 'DBInstances', ['DBInstanceIdentifier', 'DBInstanceStatus', 'AllocatedStorage'])
    collect('rds', 'describe_db_clusters', 'DBClusters', ['DBClusterIdentifier', 'Status'])
    collect('elasticache', 'describe_cache_clusters', 'CacheClusters', ['CacheClusterId', 'CacheClusterStatus'])
    collect('opensearch', 'list_domain_names', 'DomainNames', ['DomainName'])
    collect('efs', 'describe_file_systems', 'FileSystems', ['FileSystemId', 'LifeCycleState', 'SizeInBytes'])
    collect('dynamodb', 'list_tables', 'TableNames', [])
    collect('ecs', 'list_clusters', 'clusterArns', [])
    collect('redshift', 'describe_clusters', 'Clusters', ['ClusterIdentifier', 'ClusterStatus'])
    collect('sagemaker', 'list_endpoints', 'Endpoints', ['EndpointName', 'EndpointStatus'])
    collect('sagemaker', 'list_notebook_instances', 'NotebookInstances', ['NotebookInstanceName', 'NotebookInstanceStatus'])
    collect('ecr', 'describe_repositories', 'repositories', ['repositoryName', 'repositoryArn'])
    collect('logs', 'describe_log_groups', 'logGroups', ['logGroupName', 'storedBytes'])
    collect('secretsmanager', 'list_secrets', 'SecretList', ['Name', 'ARN', 'DeletedDate'])
    collect('backup', 'list_backup_vaults', 'BackupVaultList', ['BackupVaultName', 'NumberOfRecoveryPoints'])
    collect('lambda', 'list_functions', 'Functions', ['FunctionName'])
    collect('lightsail', 'get_instances', 'instances', ['name', 'state'])
    collect('fsx', 'describe_file_systems', 'FileSystems', ['FileSystemId', 'Lifecycle'])
    collect('cloudformation', 'describe_stacks', 'Stacks', ['StackName', 'StackStatus'])
    return region, result, errors


inventory = {'account': account, 'regions': {}, 'errors': {}}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    for region, resources, errors in pool.map(scan, regions):
        if resources:
            inventory['regions'][region] = resources
        if errors:
            inventory['errors'][region] = errors

inventory['global'] = {}
for service, operation, key, fields in [
    ('s3', 'list_buckets', 'Buckets', ['Name']),
    ('route53', 'list_hosted_zones', 'HostedZones', ['Id', 'Name', 'Config']),
    ('savingsplans', 'describe_savings_plans', 'savingsPlans', ['savingsPlanId', 'state', 'end']),
]:
    try:
        data = getattr(session.client(service, region_name='us-east-1', config=CONFIG), operation)()
        inventory['global'][service] = [{f: row.get(f) for f in fields} for row in data.get(key, [])]
    except Exception as exc:
        inventory['global'][service] = {'error': type(exc).__name__}
try:
    data = session.client('cloudfront', config=CONFIG).list_distributions().get('DistributionList', {})
    inventory['global']['cloudfront'] = [{k: r.get(k) for k in ('Id', 'Enabled', 'Status')} for r in data.get('Items', [])]
except Exception as exc:
    inventory['global']['cloudfront'] = {'error': type(exc).__name__}

Path('/tmp/market-intel-account-inventory.json').write_text(json.dumps(inventory, indent=2, default=str))
print(json.dumps(inventory, indent=2, default=str))
