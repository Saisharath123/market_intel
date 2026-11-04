"""Generate promtool regression tests from the ACTUAL rendered canary queries.

Usage: python deploy/rollouts/check-analysis.py rendered.yaml tests.yaml
Then: promtool test rules tests.yaml
"""

import argparse
from pathlib import Path
import re

import yaml


def series(metric, values, *, status=None, le=None, revision='canary', namespace):
    labels = {
        'namespace': namespace, 'service': 'market', 'route': '/api/v1/quotes',
        'rollouts_pod_template_hash': revision,
    }
    if status is not None:
        labels['status'] = status
    if le is not None:
        labels['le'] = le
    encoded = ','.join(f'{key}="{value}"' for key, value in labels.items())
    return {'series': metric + '{' + encoded + '}', 'values': values}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rendered', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    docs = [doc for doc in yaml.safe_load_all(args.rendered.read_text()) if doc]
    templates = [doc for doc in docs if doc['kind'] == 'AnalysisTemplate']
    if len(templates) != 1:
        raise SystemExit('Expected exactly one production canary AnalysisTemplate')
    metrics = {metric['name']: metric for metric in templates[0]['spec']['metrics']}
    expected = {
        'sufficient-canary-requests', 'canary-server-error-ratio', 'canary-p95-seconds',
    }
    if set(metrics) != expected:
        raise SystemExit('Canary metric contract changed; review query tests')
    expressions = {}
    for name, metric in metrics.items():
        condition = metric['successCondition']
        if 'len(result) == 1' not in condition or '!isNaN(result[0])' not in condition:
            raise SystemExit(f'{name} must reject missing and NaN measurements')
        if metric.get('failureLimit') != 0:
            raise SystemExit(f'{name} must fail on its first failed measurement')
        query = metric['provider']['prometheus']['query']
        if 'rollouts_pod_template_hash="{{args.canary-hash}}"' not in query:
            raise SystemExit(f'{name} must isolate the canary revision')
        expressions[name] = query.replace('{{args.canary-hash}}', 'canary')
    namespaces = re.findall(r'namespace="([^"]+)"', next(iter(expressions.values())))
    if len(namespaces) != 1:
        raise SystemExit('Expected one explicit namespace selector')
    namespace = namespaces[0]
    requests = 'market_intel_http_requests_total'
    latency = 'market_intel_http_request_seconds_bucket'

    def sample(metric, values, **labels):
        return series(metric, values, namespace=namespace, **labels)

    def case(name, inputs, count, errors, p95):
        results = {
            'sufficient-canary-requests': count,
            'canary-server-error-ratio': errors,
            'canary-p95-seconds': p95,
        }
        checks = []
        for metric, result in results.items():
            expression = expressions[metric]
            if result == 'NaN':
                # NaN cannot equal itself. Assert this property rather than
                # asking promtool to compare two IEEE NaN expected values.
                expression = f'({expression}) == bool ({expression})'
                result = 0
            checks.append({
                'expr': expression, 'eval_time': '2m',
                'exp_samples': [] if result is None else [{'labels': '{}', 'value': result}],
            })
        return {
            'name': name, 'interval': '30s', 'input_series': inputs,
            'promql_expr_test': checks,
        }

    zero = [
        sample(requests, '0+0x4', status='200'),
        sample(latency, '0+0x4', le='0.1'),
        sample(latency, '0+0x4', le='+Inf'),
    ]
    healthy = [
        sample(requests, '0+30x4', status='200'),
        sample(latency, '0+30x4', le='0.1'),
        sample(latency, '0+30x4', le='+Inf'),
        # The previous version is unhealthy; it must not pollute the canary gate.
        sample(requests, '0+300x4', status='500', revision='stable'),
        sample(latency, '0+300x4', le='10', revision='stable'),
        sample(latency, '0+300x4', le='+Inf', revision='stable'),
    ]
    unhealthy = [
        sample(requests, '0+30x4', status='200'),
        sample(requests, '0+30x4', status='500'),
        sample(latency, '0+0x4', le='1'),
        sample(latency, '0+60x4', le='5'),
        sample(latency, '0+60x4', le='+Inf'),
    ]
    tests = {
        'rule_files': [], 'evaluation_interval': '30s',
        'tests': [
            case('No traffic cannot produce a successful canary', [], 0, None, None),
            case('Zero counters produce zero volume and NaN quality', zero, 0, 'NaN', 'NaN'),
            case('Healthy canary ignores unhealthy stable metrics', healthy, 120, 0, 0.095),
            case('Bad canary exceeds error and latency limits', unhealthy, 240, 0.5, 4.8),
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(tests, sort_keys=False), encoding='utf-8')
    print(f'Generated four canary-query scenarios from {args.rendered}; run promtool on {args.output}.')


if __name__ == '__main__':
    main()
