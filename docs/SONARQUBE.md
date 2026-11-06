# Private SonarQube quality gate

This project uses **SonarQube Community Build** on a dedicated private EC2 host.
The blocking scan runs on `main`, including reusable validation before images
are released. Pull requests still run lint, tests, secret scanning, image checks
and manifest checks. Community Build supports analysis of the project's main
branch only; native pull-request/branch analysis and PR decoration require a
licensed SonarQube Server edition. This implementation does not claim that the
free edition provides those features or bypass the license with a branch plugin.

Official reference: [Community Build analysis overview](https://docs.sonarsource.com/sonarqube-community-build/analyzing-source-code/analysis-overview).

## Infrastructure and access

`infra/sonarqube.json` defines one `t3a.large` (2 vCPU / 8 GiB) in an existing
private EKS subnet. It uses the VPC's existing NAT for outbound HTTPS. There is
no public IP, inbound security-group rule, SSH key or public Sonar endpoint.
Sonar listens on host loopback port 9000; PostgreSQL has no published port.
Both container images are pinned to verified official image digests. Docker
Compose and the CI Session Manager plugin have pinned, verified SHA-256 hashes.
IMDSv2 is required, CPU credits use standard mode, and the 60 GiB gp3 root disk
is encrypted. Linux Elasticsearch limits are applied before startup.

Sonar and PostgreSQL share this one host for this portfolio lab. Sonar recommends
a physically separate database host for production. This deployment has no
automatic failover, external database backup or disaster recovery. EC2, disk,
NAT usage and the generated administrator credential in Secrets Manager incur
AWS charges. Container volumes survive service restarts and instance stops;
the root disk is deleted when the instance is terminated.

Official references: [Sonar host requirements](https://docs.sonarsource.com/sonarqube-community-build/server-installation/server-host-requirements),
[Sonar Docker example](https://github.com/SonarSource/docker-sonarqube/blob/master/example-compose-files/sq-with-postgres/docker-compose.yml).

The CI role trusts only this repository's immutable `main` OIDC subject. It can
start a session only on this exact instance with the owned
`market-intel-sonar-port-forward` Session Manager document. The document fixes
the remote port at 9000 and grants neither a shell nor arbitrary remote-port
forwarding. Its data-channel and session-cleanup permission is scoped to the
`market-intel-sonar-*` role-session prefix. It has no ECR, EKS, EC2 administration
or Sonar administrator permission. Fork pull requests receive none of these AWS
or Sonar credentials.

Official reference: [AWS session-document permissions](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-specify-session-document.html).

## Bootstrap and connect

Provision the ownership-tagged Sonar stack only after the dedicated EKS VPC and
NAT exist. Supply its `VpcId`, one private `SubnetId` and the validated existing
`GitHubProviderArn` to `infra/sonarqube.json`; use the repository's managed
provisioner so ownership and change-set checks remain in effect. Record the
non-secret stack outputs `SonarInstanceId`, `SonarTunnelRoleArn`,
`SonarSessionDocument` and `SonarAdminSecretArn`.

Wait for EC2/SSM readiness and the `market-intel-sonar` systemd unit to start.
First startup downloads the pinned images and initializes PostgreSQL and Sonar,
so a CloudFormation `CREATE_COMPLETE` result alone does not prove server health.
Use the AWS `agent` profile for local administration and verify its identity.

Install the official AWS Session Manager plugin on your workstation. The helper's
`install-plugin` action installs the pinned Ubuntu x86_64 package for hosted CI;
it intentionally does not modify a Windows workstation's software installation.

```powershell
$env:AWS_PROFILE = 'agent'
$env:AWS_REGION = 'ap-south-1'
$env:SONAR_SSM_INSTANCE_ID = '<SonarInstanceId>'
$env:SONAR_HOST_URL = 'http://127.0.0.1:9000'
python infra/sonarqube-tunnel.py start --timeout 600
```

The helper waits for the actual port-open status and Sonar `UP`, and refuses an
unrelated listener already using local port 9000. Runtime session IDs/logs stay
outside the Git checkout; they contain connection status, not secret values.

Initialize the server over the tunnel:

```powershell
python infra/sonarqube-configure.py `
  --secret-arn '<SonarAdminSecretArn>' `
  --instance-id '<SonarInstanceId>' `
  --tunnel-role-arn '<SonarTunnelRoleArn>' `
  --execute
```

The helper reads the generated administrator password from its dedicated AWS
Secret and changes Sonar's default password. It creates the private
`market-intel` project, a separate scanner account with project analysis/browse
permission, and a 90-day **project analysis token**. It removes the default global
analysis/project-creation grants from `sonar-users` and `Anyone` on this dedicated
instance; the built-in `sonar-users` membership itself cannot be narrowed.
Administrator permissions remain with `sonar-administrators`. It writes the token directly
to the GitHub `SONAR_TOKEN` secret through stdin. No credential is printed,
written into Git, passed as a command-line argument, or retained as an artifact.
The helper reuses the local saved GitHub login without writing another token.
It sets the following non-secret repository variables:

| Variable | Value |
|---|---|
| `SONAR_SSM_INSTANCE_ID` | Owned Sonar instance ID |
| `SONAR_TUNNEL_ROLE_ARN` | Dedicated OIDC tunnel role ARN |
| `SONAR_HOST_URL` | `http://127.0.0.1:9000` |
| `SONAR_EDITION` | `community` |
| `SONAR_ENABLED` | `true` (informational; mandatory gate cannot be disabled) |

Missing configuration, tunnel failure, server failure and a rejected quality
gate stop the main/release pipeline. The scan uses
`sonar.qualitygate.wait=true`; a scan submission alone is not a passed gate.
The administrator credential never goes into CI.

Official reference: [Sonar token types](https://docs.sonarsource.com/sonarqube-server/user-guide/managing-tokens).

Close the local connection after administration:

```powershell
python infra/sonarqube-tunnel.py stop
```

CI closes its tunnel in an `always()` step. If termination fails, the helper
keeps its runtime session record so cleanup can be retried; it does not kill an
arbitrary process using a stale saved PID.

## Verify and maintain

Run main CI and inspect the dedicated Sonar job, the analysis commit and the
server quality-gate result. Check that image publishing has not begun when the
gate is rejected. Record the actual workflow run and status; template validation
and an `UP` server response are not evidence that source analysis succeeded.

Open `http://127.0.0.1:9000` only while your local SSM tunnel is running. Retrieve
the administrator credential privately from its AWS Secret when needed; do not
save the password in screenshots, Git or this runbook. The scanner user has no
administrator role. Rotate the 90-day project token before it expires by
rerunning the configuration helper while no release job is using the old token.
The helper revokes its previous named token and replaces the GitHub secret.

Review security releases and upgrade the explicit image digests deliberately.
Back up PostgreSQL and test restoration before depending on analysis history.
Destroy the owned Sonar stack only when cleanup is authorized; doing so deletes
the instance's analysis database and root disk. Check Secrets Manager's recovery
window and remaining AWS resources separately when ending the lab.
