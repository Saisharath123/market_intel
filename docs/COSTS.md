# Cost estimate and teardown

Rates were read from the AWS Price List API for Mumbai on November 4, 2026. The estimate assumes Linux shared-tenancy On-Demand nodes, EKS standard support, one internal ALB and 730 running hours. It is an estimate, not an account bill or a free-tier/credit guarantee.

| Resource | Rate | 730-hour baseline (USD) |
|---|---:|---:|
| EKS standard-support control plane | $0.10/hour | $73.00 |
| Two t3a.large workers | 2 x $0.0493/hour | $71.98 |
| One NAT gateway | $0.056/hour | $40.88 |
| One NAT public IPv4 address | $0.005/hour | $3.65 |
| One internal application ALB | $0.0239/hour | $17.45 |
| SonarQube t3a.large host | $0.0493/hour | $35.99 |
| 147 GiB gp3: worker roots, monitoring and 60 GiB Sonar host | $0.0912/GB-month | $13.41 |
| One private Route 53 hosted zone | $0.50/month | $0.50 |
| **Fixed estimate** | | **$256.86/month** |

This is about $8.44 per 24 running hours at the listed rates. The November 6 extension reuses the checked EC2/EBS rates for the new Sonar host and adds private DNS. NAT data processing adds $0.056/GB and ALB usage adds $0.008 per LCU-hour. Cross-zone/internet transfer, ECR storage/scanning, CloudWatch usage, the Sonar administrator Secrets Manager secret/API requests, extra volumes/snapshots/IP addresses, taxes and support are excluded. Community Build has no software license fee; native Sonar pull-request analysis requires a paid edition. Cluster/node upgrades can temporarily add replacement capacity. The supplied T3a launch templates use standard CPU credits: sustained CPU can throttle after credits are exhausted rather than incur unlimited-mode surplus charges.

The internal ALB and private Sonar host do not require public frontend IPv4 addresses. A future internet-facing ALB, Client VPN or extra NAT gateway changes this estimate. Client VPN is not part of this baseline. The private DNS zone routes actual dashboard API requests through ALB canary weights.

Official pricing references: [EKS](https://aws.amazon.com/eks/pricing/), [EC2 On-Demand](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS](https://aws.amazon.com/ebs/pricing/), [VPC/NAT/IPv4](https://aws.amazon.com/vpc/pricing/), [ALB](https://aws.amazon.com/elasticloadbalancing/pricing/), [Route 53](https://aws.amazon.com/route53/pricing/), [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/). Exact regional compute/network rates came from their Price List API products.

## What can be paused

Stopping local previews changes no AWS charges. Scaling application replicas down reduces load but leaves workers, EKS, NAT, ALB and volumes billable. Scaling a managed node group down to zero saves worker compute but does not stop the EKS control plane, NAT or retained storage; first plan around disruption budgets and stateful workloads. EKS has no general-purpose stop/resume operation for the control plane.

## Full lab teardown

Deletion is destructive. Confirm account, region, dedicated ownership tags and the data you intend to retain before executing it. Never remove shared account infrastructure as a cost-cleanup shortcut.

1. Preserve needed source, release evidence and selected monitoring exports. Do not copy credentials or raw Kubernetes Secrets into the repository.
2. Disable reconciliation for the applications being retired and remove their desired configuration through a reviewed change. Keep the AWS Load Balancer Controller running while deleting the dedicated Ingress/ALB resources; wait until their ALBs and target groups are actually gone.
3. Remove application workloads and the dedicated monitoring releases when their stored data is no longer required. Review PVCs/PVs first: `market-intel-gp3` deliberately uses `Retain`, so deleting a PVC or the cluster can leave a billable EBS volume behind. Snapshot only if it is needed; snapshots also cost money.
4. Export any required Sonar project/database data before deleting the dedicated Sonar stack: its instance root volume is deleted on termination. Delete the owned `market-intel-sre-routing` record stack and Sonar stack before the EKS network stack; otherwise their DNS record, network interface and security group can prevent foundation deletion. Review retention of the Sonar administrator secret separately. Delete the dedicated EKS cluster and its managed CloudFormation stacks using reviewed operator tooling. Deleting worker EC2 instances individually is not a complete cleanup and a managed group may replace them.
5. Audit tagged NAT gateways, EIPs, ALBs, target groups, EBS volumes/snapshots, ECR images, log groups and IAM/OIDC roles. Remove only dedicated resources whose dependencies and retention requirements have been checked. An OIDC provider or role can be shared; do not delete it just because this project used it.
6. Verify resource inventories and billing/cost tools again after deletion settles. A successful delete command is not proof that every related billable resource disappeared.

Keep a teardown evidence record with resource names, deletion timestamps and any intentionally retained assets. Do not automatically destroy resources on deployment failure.
