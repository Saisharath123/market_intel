"""Run bounded lab traffic through the INTERNAL ALB, using a deployed image.

Requires an explicitly selected kubeconfig/context. This creates only a Job in
the selected namespace; AWS resources are managed by the infrastructure stack.
"""

import argparse
import json
import re
import subprocess
import time

PROGRAM = '''import json, os, ssl, time, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None

url=os.environ['TARGET_URL']
open_url=urllib.request.urlopen
if os.getenv('TLS_CA_FILE'):
    context=ssl.create_default_context(cafile=os.environ['TLS_CA_FILE'])
    open_url=urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect()
    ).open
deadline=time.monotonic()+int(os.environ['DURATION_SECONDS'])
ok=failed=0
while time.monotonic()<deadline:
    started=time.monotonic()
    try:
        with open_url(url, timeout=10) as response:
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
    parser.add_argument('--tls-hostname', help='Canonical DNS name from the installed ALB certificate')
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
    annotations = ingress['metadata'].get('annotations', {})
    listeners = json.loads(annotations.get('alb.ingress.kubernetes.io/listen-ports', '[{"HTTP":80}]'))
    https = any(listener.get('HTTPS') == 443 for listener in listeners)
    if https and (not args.tls_hostname or not re.fullmatch(
            r'[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+', args.tls_hostname)):
        parser.error('HTTPS ALB requires --tls-hostname with the canonical DNS name')
    if not https and args.tls_hostname:
        parser.error('--tls-hostname requires an HTTPS ALB listener')
    target_url = f'https://{args.tls_hostname}/api/v1/quotes' if https else f'http://{hostname}/api/v1/quotes'
    name = 'canary-traffic-' + str(int(time.time()))
    manifest = {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': name, 'namespace': args.namespace,
                     'labels': {'app': 'canary-traffic'}},
        'spec': {
            'ttlSecondsAfterFinished': 3600,
            'activeDeadlineSeconds': args.seconds + 60,
            'backoffLimit': 0,
            'template': {'metadata': {'labels': {'app': 'canary-traffic'}}, 'spec': {
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
                        {'name': 'TARGET_URL', 'value': target_url},
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
    if https:
        pod = manifest['spec']['template']['spec']
        pod['volumes'] = [{
            'name': 'internal-ca', 'configMap': {
                'name': 'market-intel-ca', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}],
            },
        }]
        container = pod['containers'][0]
        container['env'].append({'name': 'TLS_CA_FILE', 'value': '/var/run/market-intel/ca/ca.crt'})
        container['volumeMounts'] = [{'name': 'internal-ca', 'mountPath': '/var/run/market-intel/ca', 'readOnly': True}]
    print(kubectl('apply', '-f', '-', data=json.dumps(manifest)), end='')
    print(f'Observe: kubectl -n {args.namespace} logs -f job/{name}')
    print(f'Stop early: kubectl -n {args.namespace} delete job {name}')


if __name__ == '__main__':
    main()
