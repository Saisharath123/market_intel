# Verified Market Intel images

The project signing profile signs exact ECR digests. The operator runs
`infra/image_signature_gate.py` with the pinned AWS Signer Notation tooling
and the project AWS profile before merging a digest change. The script
requires the approved account, region, project repository, and exact
signing-profile identity. It runs strict verification with revocation
checking. `--sign-unsigned` permits signing a reviewed digest when a
signature is absent.

Provide the candidate and retained rollback values with `--source`. Apply
the generated staging policy before merging the staging promotion. Verify
the staging release, then verify the same digests for the production policy
before merging the production promotion. Keep the active and rollback
digests approved until the new release is healthy. Remove retired digests
from the allowlist through a fresh verification run.

The namespace-specific admission binding rejects container images outside
the verified allowlist, including init and ephemeral containers. Signature
and revocation checks occur when the allowlist is generated. Re-run strict
verification when replacing the allowlist or investigating a revocation.

The policy and binding are cluster-level resources managed by the project
operator. The application Argo CD project cannot grant itself these
permissions. Keep signing credentials and temporary Notation configuration
outside Git.
