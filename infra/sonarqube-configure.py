#!/usr/bin/env python3
"""Initialize private Sonar and write a limited analysis token directly to GitHub.

Run after the owned CloudFormation stack and an SSM tunnel are ready. Secret
values stay in memory and travel via request bodies/stdin, never command-line
arguments, repository files, logs or workflow artifacts.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, timedelta
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'Saisharath123/market_intel'
HOST = 'http://127.0.0.1:9000'
PROJECT = 'market-intel'
SCANNER_USER = 'market-intel-ci'
TOKEN_NAME = 'market-intel-github'


def run(command, *, data=None, env=None):
    result = subprocess.run(command, input=data, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        # Do not echo output: secret-bearing API responses may be present.
        raise RuntimeError(f'{command[0]} {command[1]} failed; no credential values were printed.')
    return result.stdout


def api(path, username, password, *, fields=None):
    basic = base64.b64encode((username + ':' + password).encode()).decode()
    body = urllib.parse.urlencode(fields).encode() if fields is not None else None
    request = urllib.request.Request(HOST + '/api/' + path, data=body,
                                     headers={'Authorization': 'Basic ' + basic})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Sonar API {path.split("?", 1)[0]} returned HTTP {error.code}; response omitted.') from None


def github_environment():
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    for name in ('GH_DEBUG', 'GIT_TRACE', 'GIT_TRACE_CURL', 'GIT_CURL_VERBOSE'):
        environment.pop(name, None)
    if environment.get('GH_TOKEN') or environment.get('GITHUB_TOKEN'):
        return environment
    raw = run(['git', 'credential', 'fill'], env=environment,
              data='protocol=https\nhost=github.com\nusername=Saisharath123\n\n')
    credential = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    if credential.get('username', '').lower() != 'saisharath123' or not credential.get('password'):
        raise RuntimeError('Saved GitHub login does not match the intended repository owner.')
    environment['GH_TOKEN'] = credential['password']
    return environment


def restrict_default_permissions(password):
    """Narrow inherited privileges using groups/grants the server actually has.

    Newer Sonar releases no longer expose Anyone as a removable global group.
    A renamed default group is also valid. Discover both instead of treating an
    absent group as an error or suppressing arbitrary API failures.
    """
    result = api('user_groups/search?ps=100', 'admin', password)
    groups = result.get('groups', [])
    if result.get('paging', {}).get('total', len(groups)) > len(groups):
        raise RuntimeError('Default-group discovery exceeded one page; review this dedicated instance before continuing.')
    defaults = [group['name'] for group in groups if group.get('default')]
    if len(defaults) != 1:
        raise RuntimeError('Expected exactly one default Sonar group; no global permissions were changed.')
    for permission in ('scan', 'provisioning'):
        result = api('permissions/groups?permission=' + permission + '&ps=100', 'admin', password)
        granted = result.get('groups', [])
        if result.get('paging', {}).get('total', len(granted)) > len(granted):
            raise RuntimeError('Permission lookup exceeded one page; refusing a partial privilege check.')
        for group in granted:
            if group.get('name') == defaults[0] or group.get('name', '').casefold() == 'anyone':
                api('permissions/remove_group', 'admin', password,
                    fields={'groupName': group['name'], 'permission': permission})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--secret-arn', required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--tunnel-role-arn', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'arn:aws:secretsmanager:ap-south-1:521595303406:secret:market-intel/sonarqube-admin-[A-Za-z0-9]+', args.secret_arn):
        raise RuntimeError('Use the dedicated stack SonarAdminSecretArn output.')
    if not re.fullmatch(r'i-[0-9a-f]{17}', args.instance_id):
        raise RuntimeError('Use the dedicated SonarInstanceId stack output.')
    if args.tunnel_role_arn != 'arn:aws:iam::521595303406:role/market-intel-sonar-tunnel':
        raise RuntimeError('Use the dedicated SonarTunnelRoleArn stack output.')
    if not args.execute:
        print('PLAN rotate default admin password from the owned AWS secret; create private project/scanner user;')
        print('PLAN issue a 90-day project-analysis token and write it directly to the GitHub SONAR_TOKEN secret;')
        print('PLAN set private SSM connection variables and SONAR_EDITION=community. No changes made.')
        return
    # Check the GitHub credential before changing anything in Sonar.
    environment = github_environment()
    identity = json.loads(run(['aws', 'sts', 'get-caller-identity', '--region', 'ap-south-1', '--output', 'json']))
    if identity.get('Arn') != 'arn:aws:iam::521595303406:user/Agent':
        raise RuntimeError('Only the approved Agent AWS identity may initialize this dedicated instance.')
    response = json.loads(run(['aws', 'secretsmanager', 'get-secret-value', '--secret-id', args.secret_arn,
                               '--region', 'ap-south-1', '--output', 'json']))
    admin = json.loads(response['SecretString'])
    password = admin['password']
    if not api('authentication/validate', 'admin', password).get('valid'):
        if not api('authentication/validate', 'admin', 'admin').get('valid'):
            raise RuntimeError('Default admin password is already changed and differs from the owned secret.')
        api('users/change_password', 'admin', 'admin',
            fields={'login': 'admin', 'previousPassword': 'admin', 'password': password})
    api('settings/set', 'admin', password, fields={'key': 'sonar.forceAuthentication', 'value': 'true'})
    # The built-in sonar-users group always contains every authenticated user;
    # its membership cannot be narrowed. Remove the default global analysis
    # and project-creation grants on this dedicated instance instead. Otherwise
    # the scanner account would inherit broader rights than its direct grants.
    restrict_default_permissions(password)
    projects = api('projects/search?projects=' + PROJECT, 'admin', password).get('components', [])
    if not projects:
        api('projects/create', 'admin', password,
            fields={'project': PROJECT, 'name': 'Market Intel', 'visibility': 'private'})
    elif len(projects) != 1 or projects[0].get('key') != PROJECT:
        raise RuntimeError('Unexpected Sonar project lookup; no broader changes made.')
    api('projects/update_visibility', 'admin', password, fields={'project': PROJECT, 'visibility': 'private'})
    # A dedicated local scanner account gets only this project's analysis/browse
    # permissions; its random login password is not retained after token issuance.
    # Sonar 2026.6+ requires all four character categories. Entropy is supplied
    # by the random suffix; the prefix guarantees this cannot fail randomly.
    scanner_password = 'Aa9!' + secrets.token_urlsafe(48)
    users = api('users/search?q=' + SCANNER_USER, 'admin', password).get('users', [])
    if any(user.get('login') == SCANNER_USER for user in users):
        api('users/change_password', 'admin', password,
            fields={'login': SCANNER_USER, 'password': scanner_password})
    else:
        api('users/create', 'admin', password,
            fields={'login': SCANNER_USER, 'name': 'Market Intel CI Scanner', 'password': scanner_password, 'local': 'true'})
    for permission in ('scan', 'user'):
        api('permissions/add_user', 'admin', password,
            fields={'projectKey': PROJECT, 'login': SCANNER_USER, 'permission': permission})
    tokens = api('user_tokens/search', SCANNER_USER, scanner_password).get('userTokens', [])
    if any(token.get('name') == TOKEN_NAME for token in tokens):
        api('user_tokens/revoke', SCANNER_USER, scanner_password, fields={'name': TOKEN_NAME})
    token = api('user_tokens/generate', SCANNER_USER, scanner_password,
                fields={'name': TOKEN_NAME, 'type': 'PROJECT_ANALYSIS_TOKEN', 'projectKey': PROJECT,
                        'expirationDate': (date.today() + timedelta(days=90)).isoformat()})['token']
    run(['gh', 'secret', 'set', 'SONAR_TOKEN', '--repo', REPOSITORY], data=token, env=environment)
    settings = {'SONAR_SSM_INSTANCE_ID': args.instance_id, 'SONAR_TUNNEL_ROLE_ARN': args.tunnel_role_arn,
                'SONAR_HOST_URL': HOST, 'SONAR_EDITION': 'community', 'SONAR_ENABLED': 'true'}
    for name, value in settings.items():
        run(['gh', 'variable', 'set', name, '--repo', REPOSITORY, '--body', value], env=environment)
    print('Configured private SonarQube and the limited GitHub analysis credential. No secrets printed.')
    print('Community Build gates main/releases; native pull-request analysis needs a licensed edition.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
