# Operations, monitoring and recovery

These procedures apply after the current revision is actually deployed. Historical October notes and saved data are not evidence of current AWS resources.

## Connect and preview

Run from the repository root. Use the `agent` AWS profile and confirm account `521595303406` and exact principal `arn:aws:iam::521595303406:user/Agent` before changing anything. The cluster administration CIDR allowlist must include the operator's current egress address. Keep kubeconfig in the sibling runtime directory, outside Git; never change the default personal kubeconfig for this exercise.

```powershell
$env:AWS_PROFILE = 'agent'
$env:KUBECONFIG = Join-Path (Resolve-Path '..') 'market-intel-runtime\kubeconfig'
aws sts get-caller-identity --profile agent
aws eks update-kubeconfig --profile agent --region ap-south-1 --name market-intel-prod --alias market-intel-sre --kubeconfig $env:KUBECONFIG
kubectl --context market-intel-sre get nodes -L topology.kubernetes.io/zone
kubectl --context market-intel-sre -n argocd get applications
kubectl --context market-intel-sre -n market-intel get pods,services,rollouts.argoproj.io
kubectl --context market-intel-sre -n market-intel port-forward service/dashboard 8502:8501
```

Open [the local production preview](http://localhost:8502) on the same machine running the port-forward. Use a separate terminal for each additional forward:

```powershell
kubectl --context market-intel-sre -n market-intel-staging port-forward service/dashboard 8503:8501
kubectl --context market-intel-sre -n argocd port-forward service/argocd-server 8443:443
kubectl --context market-intel-sre -n monitoring port-forward service/monitoring-grafana 3000:80
```

Argo CD is at `https://localhost:8443` and Grafana at `http://localhost:3000`. Repeat the environment setup in each new terminal before starting a forward. The initial Argo certificate may be self-signed. Retrieve administrator passwords privately from `argocd/argocd-initial-admin-secret` and `monitoring/market-intel-grafana-admin`, rotate them, and add organizational authentication before broader access. The bootstrap generates the Grafana credential once and reuses its owned Secret on subsequent runs; it never prints or saves the password locally. Do not save password values as evidence. A CloudShell port-forward listens inside CloudShell; it does not open localhost on your laptop.

## Monitoring boundaries

The monitoring bootstrap installs Prometheus, Grafana, Alertmanager, kube-state-metrics and node exporters. The provisioned **Market Intel - Release and API Health** dashboard switches between staging and production and shows request rate, errors, p95 latency, targets, ready pods, restarts, memory and CPU. Generate some API traffic and allow several scrapes before expecting rate charts.

HTTP `/health` and `/metrics` are excluded from application request counters. An HTTP-200 payload with `status=unavailable` does not increment HTTP 5xx metrics. Read the domain status, missing-symbol list and quote/publication timestamps when checking external feeds. This baseline does not provide a freshness SLO or a trading calendar.

Alertmanager evaluates configured rules; notification delivery requires a receiver and a tested route. An alert visible in Prometheus is not proof that anyone will be notified. Loki/Alloy, Tempo and historical monitoring data are not restored by this bootstrap. Use `kubectl logs` for current application logs. Optional OTLP application instrumentation remains disabled unless an actual collector endpoint is configured.

```powershell
kubectl --context market-intel-sre -n monitoring get pods,pvc,servicemonitors,prometheusrules
kubectl --context market-intel-sre -n market-intel get servicemonitors,prometheusrules
kubectl --context market-intel-sre -n market-intel get events --sort-by=.lastTimestamp
kubectl --context market-intel-sre -n market-intel logs service/market-api --tail=100
```

## Release failure drill

Perform deliberate failures in staging or in an explicitly scheduled canary exercise, using a bounded change that can be reverted. Capture the prior healthy digest first. Observe that a bad release fails the contract check or analysis and retains the stable version as configured. Inspect the failed Job/AnalysisRun and its logs, then revert the promotion in Git. Confirm that the previous digest is running and verify the dashboard and both API contracts again. Keep the failed run as evidence; do not relabel it successful after recovery.

An internal ALB canary test needs an in-cluster client or other permitted VPC path. A `kubectl port-forward` to a Kubernetes Service bypasses ALB weighted routing, so it is not a valid measurement of the ALB traffic split. Use the configured in-cluster traffic generator for that exercise. Metrics windows need enough traffic and time; absent data must not be interpreted as a perfect success rate.

## Resource and recovery limits

Check node CPU, memory, pod capacity and volume status before scaling or adding tooling. These T3a workers use **standard** CPU-credit mode: exhausting their credit balance can throttle sustained CPU load rather than incur unlimited-mode surplus-credit charges. Two nodes do not guarantee every service survives an entire availability-zone failure. The monitoring stateful components are single replicas and EBS is zone-bound. There is no application database to back up today; preserving Grafana/Prometheus volumes is a different concern.

Review EKS/node/add-on versions before support deadlines. A control-plane update does not automatically update every node or Helm release. Read the [cost and deletion procedure](COSTS.md) before ending the lab; closing a browser or stopping a worker does not stop the platform's charges.
