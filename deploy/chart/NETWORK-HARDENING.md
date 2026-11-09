# Opt-in application network boundaries

The staging overlay isolates the dashboard, both APIs and the PostSync smoke job. It preserves the existing API ingress rules and allows the demo providers to reach only CoreDNS. Load **values-staging-network.yaml** after **values-staging.yaml** in the staging Argo CD application. The staging TLS overlay can be loaded alongside it because both API service and target ports remain 8000.

## Production cutover

1. Review a CloudFormation change set for the owned EKS stack with **EnableDedicatedAlbSubnets=true** and all existing parameters preserved. Expect two isolated subnets, one local-only route table and two subnet associations. Reserve the new subnet pair for the internal market API load balancer.
2. Layer **values-production-network.yaml** after the normal production values. With **networkHardening.enabled** set to **false**, the additional annotation moves the internal ALB to the dedicated subnet pair. Confirm the two new subnet mappings, both target groups healthy, private DNS answers in the new ranges, and the PostSync smoke test. Existing DNS answers can stay cached briefly, so check the production route before continuing.
3. Set **networkHardening.enabled** to **true** in a separate reviewed sync. The dashboard can reach the weighted ALB and news API. The market API accepts ALB traffic and its health checks, the direct smoke checks and the Prometheus scrape. The two APIs can resolve DNS and connect outbound on TCP 443, excluding private, link-local and other reserved IPv4 ranges. Hostname restrictions would require a separate egress proxy or a policy engine that supports DNS names.

The operator's localhost-only Kubernetes port-forward should continue through the authenticated API connection. Keep access to the port-forward subresource restricted by Kubernetes RBAC.

## Verification and rollback

- Check VPC CNI network-policy support and the generated PolicyEndpoints. Wait for enforcement to converge before running negative tests. Keep cluster-wide strict startup mode outside this change.
- Require the staging PostSync smoke hook, dashboard port-forward health, API Prometheus targets and fresh demo responses. In production, require the weighted quote route, the news feed, both ALB target groups and the direct smoke health and metrics checks. Use fresh API Pods to exercise real provider connections beyond the cache.
- Verify an unlabeled, controller-managed test Job cannot reach protected app ports, and a selected app Pod cannot make a connection to a disallowed address or port. Verify at least one allowed connection from the same test context. Use reachable targets so a timeout is meaningful. Remove the test Job when done.
- Argo CD auto-prune is disabled. Reverting the overlay alone can leave four policies present. After reverting and confirming the Argo desired state, remove only **dashboard-boundary**, **market-api-boundary**, **news-api-boundary**, and **smoke-boundary** from the affected namespace if they remain. Verify the app before moving the ALB back. Keep the dedicated subnets until the ALB has fully left them.

[Amazon EKS network policy considerations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)

[AWS Load Balancer Controller subnet annotations](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/annotations/#subnets)
