# Monitoring bootstrap

This directory contains curated configuration for a new install; it does not replay raw live-object exports or restore historical data. Install after the EKS EBS CSI driver and its AWS permissions are ready. Do this before enabling the application chart's ServiceMonitors/PrometheusRules or any Prometheus-driven rollout analysis.

Pinned chart: `prometheus-community/kube-prometheus-stack` **92.11.0** (Prometheus Operator app version **v0.94.1**). Its metadata supports Kubernetes `>=1.25.0-0`; compatibility of the entire running stack still needs rollout checks. Bundled chart dependencies include kube-state-metrics 8.6.0, prometheus-node-exporter 4.60.0 and Grafana 14.0.1. Upgrade those through a reviewed chart release rather than editing live Pods.

Run from the repository root in an authenticated operator terminal:

```powershell
kubectl apply -f deploy/monitoring/storage-class.yaml
helm upgrade --install monitoring kube-prometheus-stack --repo https://prometheus-community.github.io/helm-charts --version 92.11.0 --namespace monitoring --create-namespace --values deploy/monitoring/values.yaml --wait --timeout 15m
kubectl apply -f deploy/monitoring/api-dashboard.yaml
kubectl -n monitoring get pods,pvc,services
```

The monitoring add-on is a versioned administrator bootstrap. The application release is reconciled by Argo CD; no claim is made that this Helm install is already managed by a separate Argo Application.

The expected Prometheus URL for in-cluster analysis is:

```text
http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090
```

Check the actual rendered/installed Service name before configuring consumers. Use `kubectl -n monitoring get service monitoring-kube-prometheus-prometheus`. Private UIs are accessed with port-forward:

```powershell
kubectl -n monitoring port-forward service/monitoring-grafana 3000:80
kubectl -n monitoring port-forward service/monitoring-kube-prometheus-prometheus 9090:9090
```

Grafana's local URL is `http://localhost:3000`; secure cookies are disabled only for this HTTP localhost access path. Configure its external URL, TLS, secure cookies and authentication before adding ingress. No anonymous access is enabled.

Prometheus retains up to seven days/15 GB within a 20 GiB claim. Grafana uses 5 GiB and Alertmanager 2 GiB. All claims use encrypted gp3 storage with `Retain` reclaim policy; deletion can leave volumes billable. These are single-replica stateful services, not a redundant monitoring design. Cluster-manager/scheduler/etcd scrapers that cannot reach EKS-managed endpoints are disabled.

The application dashboard discovers `market-intel` and `market-intel-staging` metrics through the chart ServiceMonitors. It requires the Grafana dashboard sidecar and Prometheus datasource supplied by the pinned stack. No alert receiver or paging integration is configured. Loki/Tempo/OTel collector installation and historical metric/log restoration are separate future work.

Source: [official chart and upgrade guidance](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack).
