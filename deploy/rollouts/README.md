# Market API canary on EKS

Production uses a focused canary for `market-api`. Dashboard and news keep normal
rolling deployments. An internal AWS ALB sends `/api/v1` traffic to stable and
canary target groups at 10%, 50%, then 100%. The dashboard's internal
market calls use `http://api.market-intel.internal`, a private CNAME for the
weighted ALB. Real dashboard requests and the bounded load job therefore follow
the same canary weights. The `market-api` and `market-api-canary` Services remain
the stable/canary backing targets. Staging keeps its direct service URL.

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
- `rollouts.routeDashboardThroughAlb: true` and `rollouts.apiHostname` set to the
  private API DNS name. The bootstrap owns the private DNS zone, waits for the
  Ingress ALB address and updates this CNAME before waiting for application
  health. On cluster recreation, refresh the CNAME to the new ALB address.
  Hostname values contain no scheme, path or port. Chart rendering rejects an
  empty or malformed hostname when weighted dashboard routing is enabled.

The ALB and two target groups cost money while provisioned. Its listener is
HTTP inside the VPC. It is not a public HTTPS endpoint. Administrative services
remain ClusterIP and are reached through authenticated local port-forwards.

## Run and inspect a release

The first deployment creates a stable revision; Argo Rollouts skips canary steps
when no prior revision exists. A subsequent **different market image digest** is
needed to exercise a normal image-release canary. A dashboard-only change does
not exercise this API rollout. The controlled abort drill below instead creates
a new pod revision using the same tested image.

Production also runs the PostSync functional hook. It checks the real provider
modes, response contracts and fetch freshness, using the weighted API origin for
quotes/insights and the direct Services for health/metrics. HTTP 200 with an
unavailable/partial/stale payload or an accidental demo provider fails this
hook. A failed PostSync hook marks the sync failed; it does not itself perform a
canary abort or revert Git. Canary metric gates execute before full promotion.

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

For a controlled drill in this portfolio lab, open a reviewed Git PR that sets
`rollouts.abortDrillId: reviewed-lab-drill-<unique-id>` in production values. Run
all ordinary PR checks. Keep every image digest unchanged. The nonempty ID adds
a pod-template annotation, creating a new revision with the same healthy image,
and adds an inline Prometheus gate whose `vector(0)` result deliberately fails
the `result[0] == 1` condition. The normal 10% step and analysis failure therefore
exercise automatic abort without introducing broken application code or
bypassing CI/staging security checks. Never enable this drill in shared
production; it intentionally blocks release progression.

Start the bounded traffic job before merging this drill PR. Preserve the failed
`controlled-abort-drill` measurement, the aborted Rollout and actual ALB
stable/canary weights (100/0). Revert the drill PR through Git to restore the
empty ID and original pod template, then verify successful reconciliation and
functional smoke. A standalone failing AnalysisRun is insufficient evidence of
a rollout abort. Record the drill as completed only after observing these
outcomes. This validates abort wiring; it is distinct from the real error/latency
query regression cases in `check-analysis.py`.

## Official references

- [Argo Rollouts ALB integration](https://argoproj.github.io/argo-rollouts/features/traffic-management/alb/)
- [Prometheus analysis](https://argoproj.github.io/argo-rollouts/analysis/prometheus/)
- [Analysis result and empty-vector handling](https://argoproj.github.io/argo-rollouts/features/analysis/)
- [Pinned chart source](https://github.com/argoproj/argo-helm/tree/argo-rollouts-2.43.8/charts/argo-rollouts)
