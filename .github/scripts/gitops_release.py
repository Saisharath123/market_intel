"""Validate and update only the three immutable GitOps image values."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

import yaml

SERVICES = {'dashboard': 'dashboard', 'market-api': 'market', 'news-api': 'news'}


def load_images(path, account, region):
    config = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    images = {}
    for service, repository in SERVICES.items():
        image = config.get('services', {}).get(service, {}).get('image', '')
        expected = rf'{re.escape(account)}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/market-intel/{repository}@sha256:[0-9a-f]{{64}}'
        if not isinstance(image, str) or not re.fullmatch(expected, image):
            raise ValueError(f'{service} must reference the approved ECR repository by SHA256 digest')
        images[service] = image
    return images


def patch_images(path, images):
    """Use YAML node positions so comments and unrelated approved settings survive."""
    path = Path(path)
    original = path.read_text(encoding='utf-8')
    before = yaml.safe_load(original)
    root = yaml.compose(original)
    services = next(value for key, value in root.value if key.value == 'services')
    replacements = []
    for service_key, service_node in services.value:
        if service_key.value in images:
            image_node = next(value for key, value in service_node.value if key.value == 'image')
            replacements.append((image_node.start_mark.index, image_node.end_mark.index,
                                 json.dumps(images[service_key.value])))
    if len(replacements) != len(SERVICES):
        raise ValueError('Target must explicitly declare all three service image fields')
    updated = original
    for start, end, value in sorted(replacements, reverse=True):
        updated = updated[:start] + value + updated[end:]
    expected = deepcopy(before)
    for name, image in images.items():
        expected['services'][name]['image'] = image
    if yaml.safe_load(updated) != expected:
        raise ValueError('Refusing a release that changes settings other than image digests')
    path.write_text(updated, encoding='utf-8', newline='')
    return updated != original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['update', 'manifest'])
    parser.add_argument('--source', required=True)
    parser.add_argument('--target')
    parser.add_argument('--account', default='521595303406')
    parser.add_argument('--region', default='ap-south-1')
    args = parser.parse_args()
    images = load_images(args.source, args.account, args.region)
    if args.command == 'manifest':
        for service, repository in SERVICES.items():
            print(f'{service}|market-intel/{repository}|{images[service].split("@", 1)[1]}')
    elif not args.target:
        parser.error('--target is required for update')
    else:
        print('Image digests updated' if patch_images(args.target, images) else 'Image digests already match')


if __name__ == '__main__':
    main()
