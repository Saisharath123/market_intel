"""Fail closed unless ECR's OS scan completed with no HIGH/CRITICAL findings."""
import json
import subprocess
import sys
import time

repository, digest = sys.argv[1:]
for attempt in range(40):
    result = subprocess.run([
        'aws', 'ecr', 'describe-image-scan-findings', '--repository-name', repository,
        '--image-id', f'imageDigest={digest}', '--output', 'json', '--no-cli-pager',
    ], capture_output=True, text=True)
    if result.returncode:
        if 'ScanNotFoundException' not in result.stderr:
            raise SystemExit('Cannot read ECR scan; release blocked')
    else:
        report = json.loads(result.stdout)
        status = report['imageScanStatus']['status']
        if status == 'COMPLETE':
            counts = report.get('imageScanFindings', {}).get('findingSeverityCounts', {})
            print(repository, json.dumps(counts, sort_keys=True))
            if counts.get('HIGH', 0) or counts.get('CRITICAL', 0):
                raise SystemExit('HIGH/CRITICAL OS findings block this release')
            break
        if status not in ('IN_PROGRESS', 'PENDING'):
            raise SystemExit(f'ECR scan status {status}; release blocked')
    time.sleep(15)
else:
    raise SystemExit('ECR scan timed out; release blocked')
