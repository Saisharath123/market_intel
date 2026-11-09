"""Verify this news pod's mTLS health without requiring Service readiness."""
from http.client import HTTPException, HTTPResponse
import json
import socket
import ssl
import sys


def healthy(host, port):
    context = ssl.create_default_context(cafile='/var/run/market-intel/ca/ca.crt')
    context.load_cert_chain('/var/run/market-intel/news-probe-client/tls.crt',
                            '/var/run/market-intel/news-probe-client/tls.key')
    with socket.create_connection(('127.0.0.1', port), timeout=2) as tcp:
        with context.wrap_socket(tcp, server_hostname=host) as tls:
            tls.sendall(f'GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode('ascii'))
            with HTTPResponse(tls) as response:
                response.begin()
                return response.status == 200 and json.loads(response.read(1024)) == {'status': 'ok', 'service': 'news'}


if __name__ == '__main__':
    try:
        if not healthy(sys.argv[1], int(sys.argv[2])):
            raise SystemExit('Unexpected news health response')
    except (OSError, ValueError, HTTPException) as error:
        raise SystemExit(f'News mTLS probe failed: {type(error).__name__}') from None
