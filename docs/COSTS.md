# Cost estimate and teardown

Rates were read from the AWS Price List API for Mumbai on November 4, 2026. The estimate assumes Linux shared-tenancy On-Demand nodes, EKS standard support, one internal ALB and 730 running hours. It is an estimate, not an account bill or a free-tier/credit guarantee.

| Resource | Rate | 730-hour baseline (USD) |
|---|---:|---:|
| EKS standard-support control plane | $0.10/hour | $73.00 |
| Two t3a.large workers | 2 x $0.0493/hour | $71.98 |
| One NAT gateway | $0.056/hour | $40.88 |
| One NAT public IPv4 address | $0.005/hour | $3.65 |
| One internal application ALB | $0.0239/hour | $17.45 |
| 87 GiB gp3: 2 x 30 GiB roots + 27 GiB monitoring | $0.0912/GB-month | $7.93 |
| **Fixed estimate** | | **$214.89/month** |

This is about $7.06 per 24 running hours at the listed rates. If worker roots are 40 GiB each, the fixed estimate becomes approximately $216.71/month. NAT data processing adds $0.056/GB and ALB usage adds $0.008 per LCU-hour. Cross-zone/internet transfer, ECR storage/scanning, CloudWatch usage, extra volumes/snapshots/IP addresses, taxes, support and optional external Sonar services are excluded. Cluster/node upgrades can temporarily add replacement capacity. The supplied T3a launch template uses standard CPU credits: sustained CPU can throttle after credits are exhausted rather than incur unlimited-mode surplus charges.

The internal ALB does not require public frontend IPv4 addresses. A future internet-facing ALB, Client VPN, extra NAT gateway or access bridge changes this estimate. The prior Client VPN/private DNS setup is not part of this baseline.

Official pricing references: [EKS](https://aws.amazon.com/eks/pricing/), [EC2 On-Demand](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS](https://aws.amazon.com/ebs/pricing/), [VPC/NAT/IPv4](https://aws.amazon.com/vpc/pricing/), [ALB](https://aws.amazon.com/elasticloadbalancing/pricing/). Exact regional rates above came from their Price List API products, not the US-region example prices in marketing tables.

## What can be paused

Stopping local previews changes no AWS charges. Scaling application replicas down reduces load but leaves workers, EKS, NAT, ALB and volumes billable. Scaling a managed node group down to zero saves worker compute but does not stop the EKS control plane, NAT or retained storage; first plan around disruption budgets and stateful workloads. EKS has no general-purpose stop/resume operation for the control plane.

## Full lab teardown

Deletion is destructive. Confirm account, region, dedicated ownership tags and the data you intend to retain before executing it. Never remove shared account infrastructure as a cost-cleanup shortcut.

1. Preserve needed source, release evidence and selected monitoring exports. Do not copy credentials or raw Kubernetes Secrets into the repository.
2. Disable reconciliation for the applications being retired and remove their desired configuration through a reviewed change. Keep the AWS Load Balancer Controller running while deleting the dedicated Ingress/ALB resources; wait until their ALBs and target groups are actually gone.
3. Remove application workloads and the dedicated monitoring releases when their stored data is no longer required. Review PVCs/PVs first: `market-intel-gp3` deliberately uses `Retain`, so deleting a PVC or the cluster can leave a billable EBS volume behind. Snapshot only if it is needed; snapshots also cost money.
4. Delete the dedicated EKS cluster and its managed eksctl/CloudFormation stacks using the reviewed operator tooling. Deleting worker EC2 instances individually is not a complete cleanup and a managed group may replace them.
5. Audit tagged NAT gateways, EIPs, ALBs, target groups, EBS volumes/snapshots, ECR images, log groups and IAM/OIDC roles. Remove only dedicated resources whose dependencies and retention requirements have been checked. An OIDC provider or role can be shared; do not delete it just because this project used it.
6. Verify resource inventories and billing/cost tools again after deletion settles. A successful delete command is not proof that every related billable resource disappeared.

Keep a teardown evidence record with resource names, deletion timestamps and any intentionally retained assets. Do not automatically destroy resources on deployment failure.
