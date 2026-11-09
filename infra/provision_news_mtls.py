"""Prepare or install private News client credentials from a Windows DPAPI vault."""
import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


NAMESPACES = {'staging': 'market-intel-staging', 'production': 'market-intel'}
SECRETS = {
    'dashboard': 'dashboard-news-client-tls',
    'smoke': 'smoke-news-client-tls',
    'prometheus': 'prometheus-news-client-tls',
    'probe': 'news-probe-client-tls',
}
LABELS = {
    'app.kubernetes.io/part-of': 'market-intel',
    'app.kubernetes.io/managed-by': 'market-intel-news-mtls',
}


class ProvisionError(RuntimeError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, *, decrypt=False):
    """Use Windows CurrentUser DPAPI with no interactive prompt."""
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), source_buffer), Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ProvisionError('Windows DPAPI could not access the credential vault')
    free = ctypes.WinDLL('kernel32').LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free(output.data)


def subject(label):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])


def identity(namespace, role):
    return f'urn:market-intel:news-client:{namespace}:{role}'


def certificate(name, issuer, key, now, days):
    return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))


def pem_pair(key, cert):
    return {
        'tls.crt': cert.public_bytes(serialization.Encoding.PEM).decode('ascii'),
        'tls.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode('ascii'),
    }


def generate(namespace, now):
    signer = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    issuer = subject(f'Market Intel News client CA ({namespace})')
    ca = (certificate(issuer, issuer, signer, now, 365)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
          .sign(signer, hashes.SHA256()))
    clients = {}
    for role in SECRETS:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (certificate(subject(f'Market Intel News {role} client'), ca.subject, key, now, 90)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity(namespace, role))]),
                               critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
                .sign(signer, hashes.SHA256()))
        clients[role] = pem_pair(key, leaf)
    return {'namespace': namespace, 'ca': pem_pair(signer, ca), 'clients': clients}


def check_pair(pair, now):
    cert = x509.load_pem_x509_certificate(pair['tls.crt'].encode('ascii'))
    key = serialization.load_pem_private_key(pair['tls.key'].encode('ascii'), password=None)
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise ProvisionError('Certificate and private key in the vault do not match')
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc - timedelta(days=7):
        raise ProvisionError('Certificate is invalid or inside the renewal window')
    return cert


def validate(bundle, namespace, now):
    if bundle['namespace'] != namespace or set(bundle['clients']) != set(SECRETS):
        raise ProvisionError('The vault is for a different News client profile')
    ca = check_pair(bundle['ca'], now)
    ca.verify_directly_issued_by(ca)
    if ca.extensions.get_extension_for_class(x509.BasicConstraints).value != x509.BasicConstraints(ca=True, path_length=0):
        raise ProvisionError('Client trust does not contain the expected dedicated CA')
    public_keys = set()
    for role, pair in bundle['clients'].items():
        leaf = check_pair(pair, now)
        leaf.verify_directly_issued_by(ca)
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        if (constraints.ca or leaf.not_valid_after_utc > ca.not_valid_after_utc
                or list(san) != [x509.UniformResourceIdentifier(identity(namespace, role))]
                or list(eku) != [ExtendedKeyUsageOID.CLIENT_AUTH]):
            raise ProvisionError('Client certificate is for a different identity or usage')
        public_keys.add(leaf.public_key().public_numbers())
    if len(public_keys) != len(SECRETS) or ca.public_key().public_numbers() in public_keys:
        raise ProvisionError('Client certificate keys are not distinct')


def manifest(bundle):
    def metadata(name):
        return {'name': name, 'namespace': bundle['namespace'], 'labels': LABELS}

    yield {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata('market-intel-news-client-ca'),
           'data': {'ca.crt': bundle['ca']['tls.crt']}}
    for role, secret in SECRETS.items():
        yield {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/tls', 'metadata': metadata(secret),
               'data': {key: base64.b64encode(value.encode('ascii')).decode('ascii')
                        for key, value in bundle['clients'][role].items()}}


def install(args, namespace, resources):
    command = [args.kubectl, '--kubeconfig', str(args.kubeconfig), '--context', args.context, '--namespace', namespace]

    def kube(*parts, data=None):
        try:
            result = subprocess.run([*command, *parts], input=data, check=False, capture_output=True,
                                    text=True, encoding='utf-8', timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisionError('Kubernetes command could not be completed') from None
        if result.returncode:
            raise ProvisionError('Kubernetes read or create was rejected')
        return result.stdout

    target = json.loads(kube('get', 'namespace', namespace, '-o', 'json'))
    if target['metadata']['uid'] != args.namespace_uid or target.get('status', {}).get('phase') != 'Active':
        raise ProvisionError('Kubeconfig namespace does not match the reviewed active namespace')
    missing = []
    for item in resources:
        name = item['metadata']['name']
        existing = kube('get', item['kind'], name, '--ignore-not-found', '-o', 'json')
        if not existing.strip():
            missing.append(item)
            continue
        current = json.loads(existing)
        labels = current['metadata'].get('labels', {})
        if (any(labels.get(key) != value for key, value in LABELS.items())
                or current.get('type') != item.get('type') or current.get('data') != item['data']):
            raise ProvisionError(f'Refusing to overwrite the existing {name}; resolve ownership or renewal first')
    if missing:
        # Create-only means a concurrent conflicting resource is never overwritten.
        # Unlike client-side apply, this creates no last-applied annotation containing credentials.
        kube('create', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': missing}))
    return len(missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=NAMESPACES, required=True)
    parser.add_argument('--state', type=Path, required=True, help='Local vault directory outside the source checkout')
    parser.add_argument('--apply', action='store_true', help='Install the existing prepared bundle in Kubernetes')
    parser.add_argument('--kubectl', default='kubectl')
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--context')
    parser.add_argument('--namespace-uid', type=lambda value: str(UUID(value)))
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('The credential vault requires Windows CurrentUser DPAPI')
    if args.state.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('The DPAPI vault must be outside the source checkout')
    if args.apply and not (args.kubeconfig and args.kubeconfig.is_file() and args.context and args.namespace_uid):
        parser.error('--apply requires the reviewed kubeconfig, context and namespace UID')
    namespace = NAMESPACES[args.environment]
    vault = args.state / f'{args.environment}-news-clients.dpapi'
    now = datetime.now(timezone.utc)
    if vault.exists():
        bundle = json.loads(dpapi(vault.read_bytes(), decrypt=True))
    elif args.apply:
        raise ProvisionError('Prepare the encrypted credential vault locally before applying it')
    else:
        bundle = generate(namespace, now)
        validate(bundle, namespace, now)
        ciphertext = dpapi(json.dumps(bundle).encode('utf-8'))
        args.state.mkdir(parents=True, exist_ok=True)
        with vault.open('xb') as file:
            file.write(ciphertext)
            file.flush()
            os.fsync(file.fileno())
    validate(bundle, namespace, now)
    if args.apply:
        print(f'News mTLS credentials verified in {args.environment}; {install(args, namespace, manifest(bundle))} resources created.')
    else:
        print(f'Local {args.environment} News mTLS vault verified. No Kubernetes resources changed.')


if __name__ == '__main__':
    try:
        main()
    except ProvisionError as error:
        raise SystemExit(str(error)) from None
    except (KeyError, OSError, TypeError, ValueError, x509.ExtensionNotFound):
        raise SystemExit('Could not verify or store the News mTLS material') from None
