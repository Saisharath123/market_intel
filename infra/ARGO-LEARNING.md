# Private access, deployment and rollback lab

The frontend is Streamlit. React has not been implemented. The Python APIs and separate deployments allow a future React dashboard without replacing ingestion services.

## Connect privately

1. Install AWS VPN Client from https://aws.amazon.com/vpn/client-vpn-download/ .
2. In your authenticated Mumbai CloudShell, use **Actions → Download file** for `/home/cloudshell-user/.market-intel-private/market-intel-private.ovpn`. This contains your client private key: keep it private, do not upload it to GitHub, chat or a shared drive. Import it into AWS VPN Client as a new profile and connect.
3. Download `/home/cloudshell-user/.market-intel-private/https-ca.crt`. Inspect its SHA256 fingerprint in CloudShell with `openssl x509 -in ~/.market-intel-private/https-ca.crt -noout -fingerprint -sha256` and compare the certificate before trusting it on your own device. Import this project HTTPS CA into your Windows current-user Trusted Root Certification Authorities using Certificate Manager. This is a trust decision for your device; perform it yourself. Never bypass a browser certificate warning.
   The initial HTTPS CA SHA256 fingerprint is `90:33:64:98:EC:99:8F:7A:C9:5B:E5:C2:82:73:6F:0B:F8:0C:EA:04:45:54:DD:FA:DC:1F:26:DE:07:71:8E:ED`.
4. Open these private addresses after connecting:
   - https://dashboard.market-intel.internal
   - https://argocd.market-intel.internal
   - https://grafana.market-intel.internal

The VPN authenticates with an individual 90-day certificate. It currently does not provide MFA or SAML. Its server and HTTPS certificates need renewal before their 365-day expiration. Preserve the CA secrets securely in Secrets Manager and maintain a certificate issuance/revocation procedure before adding more users. Do not share one client profile between people. The initial setup is intended for one operator.

## Log into Argo CD

Run the following yourself in CloudShell, without screen sharing or pasting its output into chat:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 --decode; echo
```

Use username `admin` at the private Argo CD address. Rotate the initial password privately under **User Info → Update Password**, then remove the bootstrap password secret once you have verified the new login. The automation does not read or rotate your password for you. For a team, use organizational SSO and project-scoped RBAC instead of sharing admin.

If you prefer terminal-only access, use an authenticated local AWS CLI/kubectl session with permitted network access, then `kubectl -n argocd port-forward service/argocd-server 8443:443`. The built-in server certificate differs from the ALB certificate; use the VPN HTTPS address for trusted browser TLS.

## Understand the application page

Open **Applications → market-intel**. The tree contains three Deployments, six running application Pods, Services and disruption budgets. Click a Deployment or Pod to inspect desired/live manifests, events, readiness and logs. Do not paste logs containing credentials into public issues.

- **Synced** means the live objects match Git.
- **Healthy** means the configured health checks pass; it does not prove Yahoo or RSS data is complete.
- **History and Rollback** shows previous sync revisions. With automatic self-heal enabled, make lasting rollback changes in Git.

## Release a change

1. Make a small code change in a branch and open a GitHub PR.
2. Inspect **Test and build**. It runs Python checks and container/service smoke tests.
3. Merge code to main. **Build EKS release images** builds images, publishes immutable ECR tags and checks completed scans. HIGH/CRITICAL OS findings block the release.
4. Open the proposed `release/images-<run-id>` branch PR if repository policy prevents bot PR creation. Review its three image digests and merge.
5. Watch Argo CD refresh, sync and roll out the deployments. Confirm 2/2 replicas and Synced/Healthy. Refresh the dashboard and inspect Grafana for errors.

## Roll back safely

Identify the last known-good image release PR, then open a revert PR restoring its three digest values in `deploy/chart/values-production.yaml`. Review and merge it. Watch Argo CD reconcile those previous images and confirm health. Keep the previous images in ECR during your rollback window.

Do not roll back to the original Debian images: they have the remediated HIGH findings. For a harmless practice lab, use two later releases that both passed the security gate. Do not disable self-heal or remove authentication to demonstrate rollback. Database migrations, when introduced, will need a separate backward-compatible rollback plan.

## Grafana and alerts

Retrieve the generated Grafana admin password privately in your own CloudShell:

```bash
kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

Use username `admin`, rotate its password, then create viewer accounts for readers. Explore **Kubernetes / Compute Resources / Namespace (Pods)**, **Kubernetes / Compute Resources / Node (Pods)** and deployment dashboards, selecting namespace `market-intel`. Prometheus also exposes API request counts, status codes and latency histograms under `market_intel_http_*`.

Prometheus and Alertmanager remain private; no direct public ingress. Alert rules are installed, but no email/Slack/webhook notification receiver is configured yet. An approved destination is required before adding outbound notifications.

## Where to find logs

Application logs: Argo CD → Applications → market-intel → select a Pod → Logs. From authenticated CloudShell:

```bash
kubectl -n market-intel logs deployment/market-api --all-pods=true --since=30m --timestamps
kubectl -n market-intel logs deployment/news-api --all-pods=true --since=30m --timestamps
kubectl -n market-intel logs deployment/dashboard --all-pods=true --since=30m --timestamps
```

These are container logs subject to node rotation and pod lifetime; they are not a durable archive. AWS CloudWatch Logs contains EKS control-plane logs in `/aws/eks/market-intel-prod/cluster` and VPN connection logs in `/market-intel/client-vpn`. Application logs are not shipped to CloudWatch or Grafana yet. Add Grafana Alloy + Loki, or an approved CloudWatch agent pipeline, for central retained application logs.

The initial monitoring stack persists metrics on encrypted gp3 storage but uses one Prometheus/Grafana/Alertmanager replica each. It can have downtime during a node/zone failure; HA and backups remain necessary before relying on it for business-critical operations.
