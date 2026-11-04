"""Run bounded lab traffic through the INTERNAL ALB, using a deployed image.

Requires an explicitly selected kubeconfig/context. This creates only a Job in
the selected namespace; AWS resources are managed by the infrastructure stack.
"""

import argparse
import json
import re
import subprocess
import time

PROGRAM = '''import json, os, time, urllib.request
url=os.environ['TARGET_URL']
deadline=time.monotonic()+int(os.environ['DURATION_SECONDS'])
ok=failed=0
while time.monotonic()<deadline:
    started=time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body=json.load(response)
            if response.status!=200 or not isinstance(body,dict):
                raise ValueError('Unexpected API response')
        ok+=1
    except Exception as exc:
        failed+=1
        print(type(exc).__name__, flush=True)
    if (ok+failed)%100==0:
        print(json.dumps({'requests':ok+failed,'ok':ok,'failed':failed}),flush=True)
    time.sleep(max(0,0.2-(time.monotonic()-started)))
print(json.dumps({'ok':ok,'failed':failed,'finished':True}),flush=True)
'''


def kubectl(*args, data=None):
    return subprocess.run(
        ['kubectl', *args], input=data, check=True, text=True,
        capture_output=True,
    ).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', default='market-intel')
    parser.add_argument('--image', required=True, help='Exact market image@sha256 digest')
    parser.add_argument('--seconds', type=int, default=1200)
    args = parser.parse_args()
    if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', args.image):
        parser.error('--image must use an immutable sha256 digest')
    if not 120 <= args.seconds <= 3600:
        parser.error('--seconds must be between 120 and 3600')
    ingress = json.loads(kubectl(
        '-n', args.namespace, 'get', 'ingress', 'market-api-canary', '-o', 'json',
    ))
    addresses = ingress.get('status', {}).get('loadBalancer', {}).get('ingress', [])
    if not addresses or not addresses[0].get('hostname'):
        raise SystemExit('Internal ALB is not ready; no traffic Job created.')
    hostname = addresses[0]['hostname']
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', hostname):
        raise SystemExit('Unexpected ALB hostname; no traffic Job created.')
    name = 'canary-traffic-' + str(int(time.time()))
    manifest = {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': name, 'namespace': args.namespace,
                     'labels': {'app': 'canary-traffic'}},
        'spec': {
            'ttlSecondsAfterFinished': 3600,
            'activeDeadlineSeconds': args.seconds + 60,
            'backoffLimit': 0,
            'template': {'spec': {
                'restartPolicy': 'Never',
                'automountServiceAccountToken': False,
                'securityContext': {
                    'runAsNonRoot': True, 'runAsUser': 10001,
                    'seccompProfile': {'type': 'RuntimeDefault'},
                },
                'containers': [{
                    'name': 'traffic', 'image': args.image,
                    'command': ['python', '-u', '-c', PROGRAM],
                    'env': [
                        {'name': 'TARGET_URL', 'value': f'http://{hostname}/api/v1/quotes'},
                        {'name': 'DURATION_SECONDS', 'value': str(args.seconds)},
                        {'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'},
                    ],
                    'securityContext': {
                        'allowPrivilegeEscalation': False,
                        'readOnlyRootFilesystem': True,
                        'capabilities': {'drop': ['ALL']},
                    },
                    'resources': {
                        'requests': {'cpu': '50m', 'memory': '64Mi'},
                        'limits': {'cpu': '200m', 'memory': '128Mi'},
                    },
                }],
            }},
        },
    }
    print(kubectl('apply', '-f', '-', data=json.dumps(manifest)), end='')
    print(f'Observe: kubectl -n {args.namespace} logs -f job/{name}')
    print(f'Stop early: kubectl -n {args.namespace} delete job {name}')


if __name__ == '__main__':
    main()
