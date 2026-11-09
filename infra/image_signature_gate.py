"""Verify AWS Signer signatures and prepare a namespace-scoped admission allowlist."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/scripts'))
from gitops_release import load_images  # noqa: E402


PLUGIN = 'com.amazonaws.signer.notation.plugin'
TRUST_STORE = 'aws-signer-ts'
NAMESPACES = {'staging': 'market-intel-staging', 'production': 'market-intel'}


def signing_profile(account, region, profile):
    if not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('The expected AWS account must be a 12-digit account ID')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region):
        raise ValueError('An AWS commercial Region is required')
    if not re.fullmatch(r'[A-Za-z0-9_]{2,64}', profile):
        raise ValueError('An exact AWS Signer profile name is required')
    return f'arn:aws:signer:{region}:{account}:/signing-profiles/{profile}'


def trust_policy(images, profile):
    return {
        'version': '1.0',
        'trustPolicies': [{
            'name': 'market-intel',
            'registryScopes': sorted({image.split('@')[0] for image in images}),
            'signatureVerification': {'level': 'strict'},
            'trustStores': [f'signingAuthority:{TRUST_STORE}'],
            'trustedIdentities': [profile],
        }],
    }


def admission_manifest(environment, verified_images):
    if not verified_images:
        raise ValueError('The admission allowlist requires verified images')
    policy, binding = yaml.safe_load_all(
        (ROOT / 'deploy/security/verified-images-policy.yaml').read_text(encoding='utf-8')
    )
    name = f'market-intel-verified-images-{environment}'
    policy['metadata']['name'] = name
    policy['spec']['variables'][0]['expression'] = json.dumps(sorted(set(verified_images)))
    binding['metadata']['name'] = name
    binding['spec']['policyName'] = name
    namespace = binding['spec']['matchResources']['namespaceSelector']['matchLabels']
    namespace['kubernetes.io/metadata.name'] = NAMESPACES[environment]
    return yaml.safe_dump_all((policy, binding), sort_keys=False)


def run(args, env=None):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=120, check=False)


def require_success(result, operation):
    if result.returncode:
        raise ValueError(f'{operation} failed. The signature gate is closed.')


def verify_images(args, images, profile, temporary):
    aws = [args.aws, '--region', args.region, '--no-cli-pager']
    identity = run([*aws, 'sts', 'get-caller-identity', '--query', 'Account', '--output', 'text'])
    require_success(identity, 'AWS identity check')
    if identity.stdout.strip() != args.account:
        raise ValueError('The active AWS session belongs to an unexpected account')

    env = os.environ.copy()
    # Keep all trust material and registry authentication isolated from the operator's defaults.
    for name in ('NOTATION_USERNAME', 'NOTATION_PASSWORD'):
        env.pop(name, None)
    env.update({
        'NOTATION_CONFIG': str(temporary / 'notation'),
        'NOTATION_CACHE': str(temporary / 'cache'),
        'NOTATION_LIBEXEC': str(args.notation_libexec.resolve(strict=True)),
        'AWS_REGION': args.region,
        'AWS_DEFAULT_REGION': args.region,
    })
    policy = temporary / 'trust-policy.json'
    policy.write_text(json.dumps(trust_policy(images, profile)), encoding='utf-8')
    require_success(run([args.notation, 'cert', 'add', '--type', 'signingAuthority',
                         '--store', TRUST_STORE, str(args.root_cert.resolve(strict=True))], env),
                    'AWS Signer root certificate import')
    require_success(run([args.notation, 'policy', 'import', str(policy)], env), 'Strict trust policy import')
    token = run([*aws, 'ecr', 'get-login-password'])
    require_success(token, 'ECR authentication')
    if not token.stdout.strip():
        raise ValueError('ECR returned an empty registry token')
    env.update({'NOTATION_USERNAME': 'AWS', 'NOTATION_PASSWORD': token.stdout.strip()})

    for image in images:
        verify = [args.notation, 'verify', image]
        result = run(verify, env)
        if result.returncode and args.sign_unsigned and 'no signature is associated' in result.stderr.lower():
            require_success(run([args.notation, 'sign', '--plugin', PLUGIN, '--id', profile, image], env),
                            'Signing the approved digest')
            result = run(verify, env)
        require_success(result, 'Signature verification')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--profile', required=True, help='Exact AWS Signer signing profile name')
    parser.add_argument('--source', required=True, type=Path, nargs='+',
                        help='Approved values files for the release and optionally its rollback image set')
    parser.add_argument('--environment', required=True, choices=NAMESPACES)
    parser.add_argument('--root-cert', required=True, type=Path)
    parser.add_argument('--notation-libexec', required=True, type=Path,
                        help='Verified Notation libexec directory containing the AWS Signer plugin')
    parser.add_argument('--notation', default='notation')
    parser.add_argument('--aws', default='aws')
    parser.add_argument('--sign-unsigned', action='store_true',
                        help='Explicitly sign approved unsigned images, then verify them')
    parser.add_argument('--manifest-output', type=Path, help='Write a fresh allowlist after all signatures verify')
    args = parser.parse_args()
    try:
        if args.manifest_output and args.manifest_output.exists():
            raise ValueError('Choose a fresh path for the admission allowlist')
        profile = signing_profile(args.account, args.region, args.profile)
        images = sorted({image for source in args.source
                         for image in load_images(source, args.account, args.region).values()})
        with TemporaryDirectory(prefix='market-intel-signatures-') as directory:
            verify_images(args, images, profile, Path(directory))
        if args.manifest_output:
            manifest = admission_manifest(args.environment, images)
            with args.manifest_output.open('x', encoding='utf-8', newline='\n') as output:
                output.write(manifest)
    except (OSError, ValueError, subprocess.TimeoutExpired, yaml.YAMLError) as error:
        if isinstance(error, ValueError):
            raise SystemExit(str(error)) from None
        raise SystemExit('Signature gate failed to read inputs or complete verification.') from None
    print(f'Verified {len(images)} exact image signatures for {args.environment}.')


if __name__ == '__main__':
    main()
