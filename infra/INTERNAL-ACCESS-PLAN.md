# Internal access and observability change

Target: AWS account 521595303406, ap-south-1, existing market-intel-prod cluster and 10.80.0.0/16 VPC. No internet-facing application load balancer or public DNS records.

## Access boundary

- One AWS Client VPN endpoint, certificate authentication, split tunnel, 10.90.0.0/22 client range, two subnet associations in separate availability zones. Authorization to 10.80.0.0/16 only; no default internet route and no Kubernetes API security-group widening. CloudWatch connection logs retained 30 days.
- Separate project VPN client CA and HTTPS CA. Store CA private keys in dedicated Secrets Manager secrets, never in Git or chat. Issue one named, 90-day client certificate. Imported ACM certificates for VPN server and internal HTTPS only. Server certificates expire after 365 days; explicit renewal required.
- One internal ALB, HTTPS only, TLS 1.2/1.3 security policy, ingress TCP443 exclusively from the VPN association security group. Backends use existing private pod IPs. Security-group changes only for this controller's backend traffic.
- Route 53 private zone market-intel.internal with dashboard, argocd and grafana records. Private CA trust must be installed by the operator on their own device. No browser certificate-warning bypass.
- Dashboard access requires VPN membership. Argo CD and Grafana additionally retain individual application login; anonymous Grafana disabled. Initial admin credentials must be rotated privately by the operator. No passwords placed in repository configuration.
- AWS Load Balancer Controller v3.6.0 dedicated IRSA role with the reviewed upstream policy stored in infra/aws-load-balancer-controller-policy.json. Role trust limited to kube-system/aws-load-balancer-controller in this cluster.
- EBS CSI driver dedicated IRSA role using AWS managed AmazonEBSCSIDriverPolicy, limited service-account trust kube-system/ebs-csi-controller-sa. Encrypted gp3 volumes retain data on claim deletion.
- Existing GitHub publishing role gains only ecr:DescribeImageScanFindings on its three repositories. Release fails closed on HIGH/CRITICAL OS findings, missing scans or permission errors.

## Monitoring boundary

kube-prometheus-stack chart 92.11.0 in monitoring, private ClusterIP services: Prometheus, Grafana, Alertmanager, operator, kube-state-metrics and node-exporter. The standard chart creates cluster-level read/watch RBAC, CRDs and operator reconciliation roles. Node exporter has host process/network and read-only filesystem visibility. It does not receive AWS credentials. Grafana has a generated Kubernetes Secret for its initial password. Prometheus retention 7 days/15GB with a 20Gi encrypted gp3 claim; Grafana 5Gi; Alertmanager 2Gi. This initial small stack has single monitoring replicas; node failure can cause monitoring downtime while EBS reattaches. Alert routing destinations are not configured until a recipient is supplied.

Application APIs expose bounded-cardinality Prometheus metrics. ServiceMonitors scrape them, and PrometheusRules detect unavailable targets and elevated errors. Existing Kubernetes dashboards cover CPU, memory, nodes and deployments. No application payloads or user query strings become metric labels.

## Verification and remaining operator steps

Require two Ready nodes, three deployments at 2/2, Argo Synced/Healthy, completed zero-HIGH/CRITICAL ECR scans, bound PVCs, Prometheus targets up and expected dashboards. Confirm HTTPS certificate chain and authentication rejection outside the VPN; a real end-user VPN connection requires the operator's device.

The operator must install AWS VPN Client, import their private profile, connect, and trust only the dedicated HTTPS CA on their device. Credential/private-key downloads and initial password rotation are private operator actions. For learning, use Git image-digest reverts for rollback, because Argo self-heal overwrites ad-hoc kubectl changes.

Production hardening is incremental: add SAML/MFA, offline/managed certificate lifecycle, backup and restore drills, HA observability, alert destinations and branch protection. Installing this stack alone is not a production certification.
