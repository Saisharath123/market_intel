"""Run only after reviewing and authorizing this EC2 stack in authenticated CloudShell."""
import json
from pathlib import Path
import re
import sys
import boto3

revision = sys.argv[1]
if not re.fullmatch(r'[a-f0-9]{40}', revision):
    raise SystemExit('Provide an immutable 40-character Git commit SHA')
root = Path(__file__).resolve().parent
session = boto3.Session(region_name='ap-south-1')
if session.client('sts').get_caller_identity()['Account'] != '521595303406':
    raise SystemExit('Unexpected AWS account')
template = json.loads((root / 'cloudformation.json').read_text())
bootstrap = (root / 'bootstrap.sh').read_text().split('\n', 1)[1]
script = "#!/bin/bash\nexport PROJECT_REVISION='${ProjectRevision}'\n" + bootstrap
template['Resources']['Server']['Properties']['UserData'] = {'Fn::Base64': {'Fn::Sub': script}}
response = session.client('cloudformation').create_stack(
    StackName='market-intel-ec2', TemplateBody=json.dumps(template),
    Parameters=[{'ParameterKey': 'ProjectRevision', 'ParameterValue': revision}],
    Capabilities=['CAPABILITY_IAM'],
    Tags=[{'Key': 'Project', 'Value': 'MarketIntel'}],
    OnFailure='DO_NOTHING',
)
print(response['StackId'])
