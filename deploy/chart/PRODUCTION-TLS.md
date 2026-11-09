# Production API TLS migration

The migration uses the existing private load balancer, stable and canary Services, and approved image digests. Staging and the default production configuration keep their existing behavior until an overlay is enabled.

## Prerequisites

- Create the public CA ConfigMap **market-intel-ca** with key **ca.crt** in the production namespace.
- Create distinct Kubernetes TLS Secrets **market-api-tls** and **news-api-tls**. Their leaves must have the respective full Service DNS names as SANs. The CA signing key stays outside Git and the cluster.
- Import a separate leaf for the private market hostname into ACM in the load balancer region. Supply its reviewed ARN through **internalTls.albCertificateArn** when switching. Imported certificates require planned renewal.
- Verify cluster capacity for the temporary additional API containers. Each uses the existing image, resource requests, and its own writable temporary volume.

## Apply and verify each phase

1. **Prepare.** Layer **values-production-tls.yaml** after the production and optional network overlays. Argo Rollouts still directs HTTP traffic to port 8000. Both API pods gain an HTTPS listener on port 8443, and the PostSync job verifies both new endpoints against the configured CA. Existing rollout quality checks still require real request volume. Use the bounded traffic helper if needed. Wait for both Rollout revisions, Service endpoints, and the smoke job to converge before switching.
2. **Switch.** Set **internalTls.productionPhase** to **switch** and provide the ACM ARN. The dashboard, smoke job, and Prometheus verify the new HTTPS routes. Both API listeners and both ALB frontend ports remain available during the cutover. The controller switches the weighted target groups to HTTPS on port 8443. Wait for both target groups to be healthy and confirm the private DNS name, stable/canary weights, API contracts, and scrape targets. Verify that the HTTPS clients reject a wrong CA or hostname.
3. **Retire.** Set the phase to **retire** only after verified HTTPS traffic and monitoring. The chart removes port 8000 from API Services and pods, removes ALB port 80, and restricts the network policies to the remaining API ports. This creates a Rollout revision, so keep verified HTTPS canary traffic flowing. Use **deploy/rollouts/generate-traffic.py** with **--tls-hostname** set to the private DNS name on the ACM certificate. Verify plaintext failure and all expected HTTPS paths again.

The switch preserves both server listeners, but the ALB and Rollouts controllers reconcile separately. HTTPS target groups may be replaced and register asynchronously. Plan a bounded interruption window and monitor the existing route until the new target groups are healthy. Keeping an HTTP frontend does not preserve HTTP backend routing after the target group changes.

The dashboard verifies the certificates presented by the ALB and news API. The ALB encrypts connections to market pods. ALB target-group TLS does not validate the server certificate. Same-namespace service probes and Prometheus validate market pod certificates independently.

## Rollback

- From **switch**, restore **prepare** with both API ports and wait for the HTTP target groups and PostSync smoke check to recover. Keep the certificate and CA mounts available.
- From **retire**, restore **switch** first and wait until both pod listeners and both Service ports are available. Then restore **prepare** if the ALB must return to HTTP.
- From **prepare**, remove the overlay only after the stable HTTP route and monitoring are healthy. The optional canary traffic NetworkPolicy may remain if Argo automatic pruning is disabled. Delete only the confirmed stale policy after the desired state has reverted.

[Amazon EKS VPC CNI port constraints](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)

[Application Load Balancer target-group TLS behavior](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html)
