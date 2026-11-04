# Release walkthrough

This runbook describes the prepared release path. It is not a record that the workflows or cluster have succeeded. The current revision must first be reviewed, pushed and bootstrapped.

## One change from source to release

1. Create a source branch and open a pull request. **Test and build** (`ci.yml`) runs Python checks, tests, container integration smoke checks and chart validation. Review the Gitleaks/Trivy results as configured in the current workflow. Configure repository branch protection to require the intended checks; a workflow file does not create protection rules.
2. Merge the source change. **Build EKS release images** (`eks-images.yml`) builds dashboard, market and news images, applies the image security gate, publishes to the three private ECR repositories and proposes staging digest updates in `deploy/chart/values-staging.yaml`.
3. Review and merge that staging release pull request. Argo CD reconciles `market-intel-staging`. Wait for **Synced**, **Healthy**, and successful contract/smoke verification. Staging deliberately uses demo provider modes.
4. Run **Promote staging to production** (`promote-production.yml`) only after checking the staged release. Review its production digest pull request. It must copy the already tested digests to `values-production.yaml` without rebuilding images.
5. Merge the promotion. Argo CD reconciles production. Observe the market-API rollout, analysis runs, dashboard behavior and provider payloads. A failed analysis must stop promotion; investigate rather than forcing success.
6. Record the source commit, image digests, release commit, workflow run URLs and observed rollout result. Failed or unexecuted gates remain visible in that record.

Argo CD's documented CI pattern is to build images and commit changed deployment configuration. CI therefore does not need a deployment credential for the Argo server. [Argo CD CI automation](https://argo-cd.readthedocs.io/en/stable/user-guide/ci_automation/)

## Configuration before enabling publishing

| Setting | Purpose |
|---|---|
| `AWS_REGION=ap-south-1` | ECR region |
| `EKS_IMAGE_ROLE_ARN` | Exact restricted AWS OIDC image-publishing role |
| `EKS_RELEASE_ENABLED=true` | Enable the prepared EKS image-release workflow after bootstrap |
| `SONAR_ENABLED=true` | Optional later Sonar analysis; set only after its connection works |
| `SONAR_HOST_URL` | Approved, runner-reachable Sonar server address |
| `SONAR_TOKEN` secret | Sonar credential, stored in GitHub Actions secrets |

No secret is required for Yahoo/RSS demo/production modes. Kite credentials, if introduced later, belong in runtime secret management. Never put AWS keys, kubeconfig, API tokens or live Kubernetes Secret exports in source, workflow artifacts or screenshots.

Sonar being disabled is not evidence of a passed quality gate. Its server, project and token are external prerequisites; no Sonar service is provisioned by this baseline. GitHub organization policy may prevent a bot from creating a pull request. Preserve the generated release branch and open its review manually using the comparison link; do not bypass policy by auto-merging.

## Collect evidence

Run these read-only checks in an authenticated local terminal. Replace a run ID deliberately rather than copying an unrelated historical run.

```powershell
gh run list --workflow ci.yml --limit 5
gh run list --workflow eks-images.yml --limit 5
gh run view <run-id> --log-failed
aws eks update-kubeconfig --region ap-south-1 --name market-intel-prod
kubectl -n argocd get applications
kubectl -n market-intel-staging get pods,jobs
kubectl -n market-intel get pods,rollouts.argoproj.io,analysisruns.argoproj.io
kubectl -n market-intel get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].image}{"\n"}{end}'
```

Compare the running image digest references against the merged values file and ECR metadata. A successful build alone does not prove deployment; **Synced** alone does not prove application health. Smoke-test the API payload contract as well as process probes, and inspect actual quote/news source timestamps in production.

## Recovery

Recover the desired version by reverting the image-promotion commit in a reviewed Git pull request. After merge, verify Argo reconciliation, rollout status and the functional checks again. Record the incident trigger and both release digests. Do not roll back to a digest already rejected by the vulnerability policy.

Argo CD's direct rollback is unavailable with automated sync enabled; Git recovery preserves the source of truth. Argo Rollouts aborts are a separate runtime protection and do not rewrite Git or repair a bad desired release. [Argo CD automated sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/), [Argo Rollouts analysis](https://argoproj.github.io/argo-rollouts/features/analysis/)
