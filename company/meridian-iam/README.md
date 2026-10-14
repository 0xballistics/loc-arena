# meridian-iam

Central IAM for the Meridian company world: the operation-manifest contracts (`meridian_iam.contracts`), the
transactional core (`meridian_iam.core.Iam`), an HTTP service over it, a stdlib client, and the guard and adapters a
service uses to check it before doing work. Contract and status: [`docs/iam-service/spec.md`](../../docs/iam-service/spec.md);
how to work on it: [`docs/iam-service/agent.md`](../../docs/iam-service/agent.md).

This is a platform package, not one of the seven repos the agents under test work on: it is neither copied into their
checkout nor graded, and a work repo must not import it.

```sh
uv run pytest company/meridian-iam -q
```

Bootstrap methods on `Iam` (`add_principal`, `register_resource`, `import_credential`, ...) are trusted startup
authority and are never exposed to agents. Everything else authenticates a bearer credential and takes no
caller-supplied principal. Time is an injected integer-second clock.
