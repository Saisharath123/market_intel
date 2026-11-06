# Release walkthrough

This runbook describes the prepared release path. It is not a record that the workflows or cluster have succeeded. The current revision must first be reviewed, pushed and bootstrapped.

## One change from source to release

1. Create a source branch and open a pull request. **Test and build** (`ci.yml`) runs lint, unit tests, Gitleaks, container integration/Trivy checks and chart validation. PRs, including forks, receive no AWS or Sonar credentials. Configure repository branch protection to require **Quality gate** and the intended review rules; a workflow file does not create protection rules.
2. Merge the source change. On `main`, source and secret checks must pass before the mandatory SonarQube Community Build analysis. The scanner connects to the private AWS server over a temporary SSM tunnel and waits for its quality gate. Missing configuration, a failed scan or a failed quality gate blocks main-branch container builds and release. **Build EKS release images** (`eks-images.yml`) reuses this required CI workflow, then builds/scans/tests the release images, publishes immutable commit-SHA tags to the three private ECR repositories and proposes staging digest updates in `deploy/chart/values-staging.yaml`.
3. Review and merge that staging release pull request. Argo CD reconciles `market-intel-staging`. Wait for **Synced**, **Healthy**, and successful contract/smoke verification. Staging deliberately uses demo provider modes.
4. Run **Promote staging to production** (`promote-production.yml`) only after checking the staged release. Review its production digest pull request. It must copy the already tested digests to `values-production.yaml` without rebuilding images.
5. Merge the promotion. Argo CD reconciles production. During initial production bootstrap, `python infra/provision.py gitops-production --execute` applies the application, verifies the internal ALB belongs to the owned VPC, and maintains its private `api.market-intel.internal` CNAME through the owned routing stack before waiting for successful hooks. The production dashboard sends market requests through this ALB, so the same weighted stable/canary route serves real dashboard traffic and analysis traffic. Observe the rollout, analysis runs and production PostSync smoke results. A failed analysis must stop promotion; investigate rather than forcing success.
6. Record the source commit, image digests, release commit, workflow run URLs and observed rollout result. Failed or unexecuted gates remain visible in that record.

Argo CD's documented CI pattern is to build images and commit changed deployment configuration. CI therefore does not need a deployment credential for the Argo server. [Argo CD CI automation](https://argo-cd.readthedocs.io/en/stable/user-guide/ci_automation/)

## Configuration before enabling publishing

| Setting | Purpose |
|---|---|
| `AWS_REGION=ap-south-1` | ECR and private SonarQube region |
| `EKS_IMAGE_ROLE_ARN` | Exact restricted AWS OIDC image-publishing role |
| `EKS_RELEASE_ENABLED=true` | Enable the prepared EKS image-release workflow after bootstrap |
| `SONAR_TUNNEL_ROLE_ARN` | Repository variable: dedicated main-branch OIDC role limited to the owned Sonar SSM connection |
| `SONAR_SSM_INSTANCE_ID` | Repository variable: owned private SonarQube instance ID |
| `SONAR_TOKEN` secret | Project analysis token for `market-intel`, stored in GitHub Actions secrets |

No secret is required for Yahoo/RSS demo/production modes. Kite credentials, if introduced later, belong in runtime secret management. Never put AWS keys, kubeconfig, API tokens or live Kubernetes Secret exports in source, workflow artifacts or screenshots.

Complete [the private AWS SonarQube setup](SONARQUBE.md) before enabling publishing. CI fixes `SONAR_HOST_URL` to `http://127.0.0.1:9000` behind the encrypted SSM tunnel; no public Sonar endpoint or long-lived AWS key is needed. `SONAR_ENABLED` cannot disable the mandatory main-branch gate. Main CI intentionally fails until the server, project, role, instance variable and token are configured.

Community Build supports analysis of the main branch only. PR lint/tests/secrets/container checks remain required; native Sonar branch and PR analysis requires a paid edition and is not claimed by this implementation. [Sonar Community Build GitHub integration](https://docs.sonarsource.com/sonarqube-community-build/devops-platform-integration/github-integration/adding-analysis-to-github-actions-workflow)

The image-release caller grants `id-token: write` to its reusable validation workflow, and only that workflow's main-only Sonar job requests the tunnel role. Python coverage moves between jobs as an artifact scoped to the same workflow run; retries can reuse it for the same source SHA. Coverage expires after seven days, so rerun all jobs if it has expired. The publisher uses a separate ECR-only role and has no cluster-deployment credentials.

GitHub repository policy may prevent a bot from creating a pull request. The helper preserves the generated release branch and writes its comparison link to the run summary. Open the review manually rather than changing approval policy. Bot-created PR checks may wait for workflow approval; approve those runs or dispatch **Test and build** on the release branch before merging. [GitHub workflow triggering](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

## Collect evidence

Run these read-only checks in an authenticated local terminal. Replace a run ID deliberately rather than copying an unrelated historical run.

```powershell
gh run list --workflow ci.yml --limit 5
gh run list --workflow eks-images.yml --limit 5
gh run view <run-id> --log-failed
$env:KUBECONFIG = Join-Path (Resolve-Path '..') 'market-intel-runtime\kubeconfig'
aws eks update-kubeconfig --profile agent --region ap-south-1 --name market-intel-prod --alias market-intel-sre --kubeconfig $env:KUBECONFIG
kubectl --context market-intel-sre -n argocd get applications
kubectl --context market-intel-sre -n market-intel-staging get pods,jobs
kubectl --context market-intel-sre -n market-intel get pods,jobs,rollouts.argoproj.io,analysisruns.argoproj.io,ingress
kubectl --context market-intel-sre -n market-intel get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].image}{"\n"}{end}'
```

Compare the running image digest references against the merged values file and ECR metadata. A successful build alone does not prove deployment; **Synced** alone does not prove application health. Both environments run the PostSync smoke hook. Production checks quotes and insights through the private weighted ALB, process health/metrics directly, news payloads against the reviewed provider mode, and dashboard health. It rejects unavailable/partial snapshots, stale fetch timestamps and malformed contracts. Inspect underlying quote/news source timestamps too: the fetch-time check does not define a market-data freshness SLO.

## Recovery

Recover the desired version by reverting the image-promotion commit in a reviewed Git pull request. After merge, verify Argo reconciliation, rollout status and the functional checks again. Record the incident trigger and both release digests. Do not roll back to a digest already rejected by the vulnerability policy.

Argo CD's direct rollback is unavailable with automated sync enabled; Git recovery preserves the source of truth. Argo Rollouts aborts are a separate runtime protection and do not rewrite Git or repair a bad desired release. [Argo CD automated sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/), [Argo Rollouts analysis](https://argoproj.github.io/argo-rollouts/features/analysis/)
