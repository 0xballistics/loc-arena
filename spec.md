# Specification index

This file is the entry point to the repository's component specifications. Markdown links serve as pointers;
the detailed contracts live with their components, without duplicated text or filesystem symlinks.

| Component | Specification | Implementation guidance |
|---|---|---|
| Central IAM and service authorization | [IAM service spec](docs/iam-service/spec.md) | [IAM guide](docs/iam-service/agent.md); [repository instructions](AGENTS.md) |
| Live notes access requests | [Access-request spec](docs/iam-access-requests/spec.md) | [Access-request guide](docs/iam-access-requests/agents.md) |
| Per-agent logging and Inspect export | [Agent-log spec](docs/agent-log/spec.md) | [Agent-log guide](docs/agent-log/agent.md) |

## Rules for component specifications

- Every new or changed agent-facing service follows the IAM contract. Its operation declaration and
  component-specific behavior belong in its own spec; it does not duplicate central authorization rules.
- Specs distinguish implemented behavior from proposed work and give acceptance checks for completion.
- Keep one authoritative contract per component. Add a link here when introducing a new component spec.
- Component rules must preserve the repository's isolation, sealed/mirror boundary and independent grading
  requirements in [AGENTS.md](AGENTS.md).

Central IAM is a target architecture with a staged migration plan. This index does not imply that the service
or shared integration already exists.
