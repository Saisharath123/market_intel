# Market API canary on EKS

Production uses a focused canary for `market-api`. Dashboard and news keep normal
rolling deployments. An internal AWS ALB sends `/api/v1` traffic to stable and
canary target groups at 10%, 50%, then 100%. The dashboard's internal
`http://market-api:8000` calls deliberately stay on the stable version until the
canary is promoted. Demonstrate weighted traffic through the ALB, not through a
port-forward to the stable Service.

Prerequisites, installed by the cluster bootstrap:

- AWS Load Balancer Controller, private tagged subnets, and an appropriate IAM role.
- `argo/argo-rollouts` Helm chart **2.43.8**, application **v1.10.1**, using
  `controller-values.yaml`. The controller has a separate service-account IAM role
  granting only the read-only policy in `aws-readonly-policy.json`.
- Prometheus Operator and the API ServiceMonitor. The monitor scrapes every
  revision pod once through `market-api-metrics`; it exports the sanitized
  `rollouts_pod_template_hash` label and preserves the application's `service`
  label with `honorLabels: true`.
- `rollouts.enabled: true`, `monitoring.enabled: true` in production values.

The ALB and two target groups cost money while provisioned. Its listener is
HTTP inside the VPC. It is not a public HTTPS endpoint. Administrative services
remain ClusterIP and are reached through authenticated local port-forwards.

## Run and inspect a release

The first deployment creates a stable revision; Argo Rollouts skips canary steps
when no prior revision exists. A subsequent **different market image digest** is
needed to exercise a canary. A dashboard-only change does not exercise this API
rollout.

Start traffic before merging the production image promotion PR. Read the exact
market image digest from the reviewed values file and use it as `--image`:

```powershell
python deploy/rollouts/generate-traffic.py --image <market-image-at-sha256-digest>
kubectl -n market-intel get rollouts,analysisruns,ingress
kubectl -n market-intel get rollout market-api -w
kubectl -n market-intel get ingress market-api-canary -o yaml
```

The Job makes up to five requests per second for twenty minutes through the ALB.
It continues after HTTP failures so a bad canary produces enough error samples
for analysis. Its logs show counts; Job completion alone is not a success gate.
The upper limit is one hour and completed Jobs expire after one hour.

At each weight, the rollout waits two minutes, then takes three samples thirty
seconds apart. Every sample must have at least 20 canary quote requests in the
last two minutes, at most 1% HTTP 5xx errors, and p95 latency below two seconds.
The queries select the **new pod hash** and namespace. Missing/NaN samples,
insufficient traffic, or a failing SLO cannot pass. These are deliberately
conservative lab thresholds; production thresholds need measured baselines and
an agreed SLO. An upstream quote provider's delayed data or an error encoded in
an HTTP 200 JSON payload is not measured by this HTTP availability gate.

AWS target-group verification checks that ALB weights and Pod IP registration
have converged before progression. A missing IAM permission stops promotion.
Argo CD ignores only the Rollouts-managed action annotation and Service hash
selectors, preventing the two controllers from fighting over those fields.

## Failure and durable recovery

Inspect the failed AnalysisRun and its measurements. An unhealthy canary is
aborted, ALB traffic returns to the stable revision, and production's desired
image in Git still points at the attempted release. Revert the production
promotion commit through a PR so Git records the known-good desired digest.
Do not describe an in-cluster abort as a completed GitOps rollback.

```powershell
kubectl -n market-intel describe rollout market-api
kubectl -n market-intel get analysisruns
kubectl -n market-intel describe analysisrun <name>
```

For a controlled failure drill, build a dedicated test branch whose market API
returns HTTP 500 for the quote route while `/health` still succeeds. Send it
through the ordinary CI, staging, and explicitly reviewed production promotion.
Run the bounded traffic Job, retain the failed analysis and ALB-weight evidence,
then revert the promotion PR. Do not push a deliberately broken release into a
shared production environment. Record this drill as completed only after it has
actually run.

## Official references

- [Argo Rollouts ALB integration](https://argoproj.github.io/argo-rollouts/features/traffic-management/alb/)
- [Prometheus analysis](https://argoproj.github.io/argo-rollouts/analysis/prometheus/)
- [Analysis result and empty-vector handling](https://argoproj.github.io/argo-rollouts/features/analysis/)
- [Pinned chart source](https://github.com/argoproj/argo-helm/tree/argo-rollouts-2.43.8/charts/argo-rollouts)
