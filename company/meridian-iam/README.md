# meridian-iam

Central IAM for the Meridian company world. This package starts with its contracts: the operation manifest every
service ships once (`meridian_iam.contracts`), the typed `Decision`, and the failure types a service maps to
401, 403, 400, 409 and 503. Contract and rollout: [`docs/iam-service/spec.md`](../../docs/iam-service/spec.md).

This is a platform package, not one of the seven repos the agents under test work on: it is neither copied into their
checkout nor graded, and a work repo must not import it.

```sh
uv run pytest company/meridian-iam -q
```
