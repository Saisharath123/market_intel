"""Record and verify private build provenance in signed OCI image metadata."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

import yaml

from image_signature_gate import PLUGIN, load_images, prepare_notation, require_success, run, signing_profile


BUILDER = '.github/workflows/eks-images.yml@refs/heads/main'
SOURCE_REVISION = re.compile(r'[0-9a-f]{40}')
SERVICES = ('dashboard', 'market-api', 'news-api')
MAX_REVISION_CANDIDATES = 6


def release_identity(env, record, operator_repository_id=None):
    if record and operator_repository_id is not None:
        raise ValueError('Only verification may use an operator-provided repository ID')
    repository_id = operator_repository_id or env.get('GITHUB_REPOSITORY_ID', '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', repository_id):
        raise ValueError('The immutable GitHub source repository ID is required')
    if operator_repository_id is None and env.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Release provenance requires the main branch')
    if not record:
        return repository_id, None, None
    workflow = f"{env.get('GITHUB_REPOSITORY', '')}/{BUILDER}"
    revision = env.get('GITHUB_SHA', '')
    run_id = env.get('GITHUB_RUN_ID', '')
    if env.get('GITHUB_WORKFLOW_REF') != workflow or not SOURCE_REVISION.fullmatch(revision):
        raise ValueError('Build provenance must come from the release workflow and its checked-out commit')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', run_id):
        raise ValueError('A GitHub build run ID is required')
    return repository_id, revision, run_id


def common_release_revisions(args, images):
    revisions = None
    for image in images.values():
        name, digest = image.split('@')
        result = run([args.aws, '--region', args.region, '--no-cli-pager', 'ecr', 'describe-images',
                      '--repository-name', name.split('/', 1)[1], '--image-ids', f'imageDigest={digest}',
                      '--query', 'imageDetails[0].imageTags', '--output', 'json'])
        require_success(result, 'Release source-tag lookup')
        tags = json.loads(result.stdout)
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError('Every candidate image must have an immutable release tag')
        candidates = {tag for tag in tags if SOURCE_REVISION.fullmatch(tag)}
        revisions = candidates if revisions is None else revisions & candidates
    return revisions


def metadata_flags(metadata):
    return [argument for name, value in metadata.items() for argument in ('--user-metadata', f'{name}={value}')]


def verify_release(args, images, repository_id, revision, run_id, profile, env):
    for service, image in images.items():
        metadata = {
            'market-intel.provenance-version': '1',
            'market-intel.source-repository-id': repository_id,
            'market-intel.source-revision': revision,
            'market-intel.builder': BUILDER,
            'market-intel.service': service,
        }
        verify = [args.notation, 'verify', *metadata_flags(metadata), image]
        result = run(verify, env)
        if result.returncode and service in args.sign_new_service:
            # A freshly built image may already carry a valid manual signature on identical bytes.
            # Refuse to sign over invalid, revoked or unreadable signatures.
            signature = run([args.notation, 'verify', image], env)
            if signature.returncode and 'no signature is associated' not in signature.stderr.lower():
                require_success(signature, 'Pre-sign signature verification')
            metadata['market-intel.build-run-id'] = run_id
            require_success(run([args.notation, 'sign', '--force-referrers-tag=false', '--plugin', PLUGIN, '--id', profile,
                                 *metadata_flags(metadata), image], env), 'Build provenance signing')
            result = run(verify, env)
        require_success(result, 'Build provenance and revocation verification')


def verify_staged_revisions(args, images, repository_id, revisions, profile, env):
    # A content-identical build can share a digest with an older release tag.
    # Pick a revision only when all three signatures attest to that revision.
    if not revisions or len(revisions) > MAX_REVISION_CANDIDATES:
        raise ValueError('The staged source is missing or ambiguous; supply the reviewed source revision')
    for revision in sorted(revisions):
        try:
            verify_release(args, images, repository_id, revision, None, profile, env)
        except ValueError:
            continue
        return
    raise ValueError('No common staged source revision has valid, unrevoked provenance for all images')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('record', 'verify'))
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--profile', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--notation', required=True)
    parser.add_argument('--notation-libexec', type=Path, required=True)
    parser.add_argument('--root-cert', type=Path, required=True)
    parser.add_argument('--aws', default='aws')
    parser.add_argument('--repository-id', help='Use the reviewed immutable GitHub repository ID for operator verification')
    parser.add_argument('--source-revision', help='Require one reviewed source revision in verify mode')
    parser.add_argument('--sign-new-service', choices=SERVICES, action='append', default=[],
                        help='A service freshly built, tested and pushed in this release job')
    args = parser.parse_args()
    try:
        record = args.mode == 'record'
        if not record and args.sign_new_service:
            raise ValueError('Verify mode cannot sign candidate images')
        if args.source_revision and (record or not SOURCE_REVISION.fullmatch(args.source_revision)):
            raise ValueError('A source revision override requires verify mode and a full Git SHA')
        repository_id, revision, run_id = release_identity(os.environ, record, args.repository_id)
        profile = signing_profile(args.account, args.region, args.profile)
        images = load_images(args.source, args.account, args.region)
        revisions = common_release_revisions(args, images)
        if record and revision not in revisions:
            raise ValueError('A release digest is not tagged with the current build revision')
        if args.source_revision:
            revisions &= {args.source_revision}
        with TemporaryDirectory(prefix='market-intel-release-') as directory:
            env = prepare_notation(args, images.values(), profile, Path(directory))
            if record:
                verify_release(args, images, repository_id, revision, run_id, profile, env)
            else:
                verify_staged_revisions(args, images, repository_id, revisions, profile, env)
    except (OSError, ValueError, subprocess.TimeoutExpired, yaml.YAMLError) as error:
        if isinstance(error, ValueError):
            raise SystemExit(str(error)) from None
        raise SystemExit('Build provenance gate could not complete verification.') from None
    print('Verified private build provenance and signature revocation status for all three release images.')


if __name__ == '__main__':
    main()
