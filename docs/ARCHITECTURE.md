# Architecture and scope

This is the proposed EKS delivery architecture for the existing stock-market application. Configuration in Git is reviewable implementation material, not proof of a running deployment.

## Application

The dashboard, market API and news API are three separate containers with internal HTTP interfaces. They share a Python repository and selected helper modules; they are not independent business systems with separate databases. The market API normalizes six NSE/index quotes, computes changes against the prior close and exposes watchlist insights. The news API reads bounded RSS content, removes duplicate links and reports publication freshness. The dashboard calls both APIs and displays missing data explicitly.

Staging uses one replica per service with demo data. Production uses real Yahoo/RSS provider modes and two replicas per service where the chart configures them. Neither environment stores a durable history. Cache state and Streamlit session state are process-local. A fresh process can be healthy while an upstream provider is unavailable.

## Proposed infrastructure

- EKS `market-intel-prod`, Mumbai (`ap-south-1`), Kubernetes 1.36.
- Two private managed `t3a.large` workers, a dedicated VPC and subnets across two availability zones.
- One NAT gateway for the learning environment. This reduces fixed cost but introduces an egress dependency on one zone and possible cross-zone transfer charges.
- A restricted public Kubernetes administration endpoint plus private endpoint access. Public API access means authenticated, CIDR-restricted cluster administration; it does not publish the applications.
- Private ECR repositories for dashboard, market and news images. GitHub holds source/configuration; ECR holds runtime images.
- `market-intel-staging` and `market-intel` namespaces, managed by separate Argo CD Applications.
- An internal application ALB for market-API canary traffic, including actual production dashboard requests through the owned `api.market-intel.internal` private DNS record. Administrative UIs and local previews use authenticated `kubectl port-forward`.
- A separate private `t3a.large` SonarQube Community Build/PostgreSQL host with encrypted storage and no inbound ports. Hosted CI reaches its loopback-only port through a repository/main-scoped OIDC role and a fixed-port SSM document.

The EKS managed control plane is highly available. Two workers, one NAT gateway, single-replica monitoring and the absence of application persistence do not establish end-to-end high availability. Node placement and workload distribution must be checked after creation.

EKS 1.36 is under standard support through August 2, 2027; 1.37 is the latest standard version in the November 4, 2026 documentation. Keeping 1.36 is a deliberate compatibility choice for the saved project, with an upgrade review before that date. [AWS version calendar](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)

## Trust and delivery boundaries

GitHub Actions obtains short-lived AWS credentials through OIDC, restricted to the exact repository and permitted branch/environment. Its publishing role can write the dedicated ECR repositories; it does not administer Kubernetes. Argo CD reads approved Git state and reconciles Kubernetes. The app containers need neither AWS access keys nor a Kubernetes service-account token.

Source checks run before image builds. Community Build adds mandatory main/release analysis, while PRs run tests, secrets, image and manifest checks without AWS/Sonar credentials. The separate Sonar role cannot publish images or administer EKS. Its project-analysis token is independent of the administrator credential; native PR analysis needs a licensed edition.

Repository names alone may not be sufficient in an OIDC trust policy: GitHub's current subject format can contain immutable owner/repository IDs. Preserve and verify the actual repository subject instead of widening it with wildcards when authentication fails. [GitHub OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)

Git is the durable record of the desired release. A promotion changes image digests, not application source. Staging and production consume the same built images. Rollouts can shift market-API traffic and evaluate Prometheus measurements; chart configuration and a successful analysis are separate evidence.

## Honest portfolio boundaries

The useful engineering demonstrations are a traceable release, working integration checks, least-privilege publishing, immutable promotion, a failed canary exercise, and recovery to the previous Git release. No current claim is made for a trading engine, tick-stream feed, licensed public quote redistribution, durable ingestion, event replay, database backup, production SLA or cross-region disaster recovery. Those are possible later increments with separate requirements.

The existing provider implementation polls and caches for five minutes per process. A browser refresh every 30 seconds does not turn that source into a 30-second exchange feed. Quote age and market holidays must be interpreted separately; the 36-hour age warning is not an exchange calendar.
