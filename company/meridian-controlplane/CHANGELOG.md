# Changelog — meridian-controlplane

## 0.6.0
- agents: executions, runs of an agent's latest version on the company's runners, queued until one is free, and
  a `run` right to start and cancel them.

## 0.5.1
- agents: a `CentralAuthority` protocol, so the service can run under a central authorization service handed in by
  its deployment; the package itself imports none.

## 0.5.0
- agents: an agents service holding the registry of the agents Meridian runs, with rights per caller.

## 0.4.0
- rbac: roles over meridian-common scopes and an RbacEngine with resource-namespace matching.
- scaffold: validated agent-roster, spawn-cap, and tool-grant config.

## 0.3.0
- identity: durable service accounts, a rotation policy, and a durable-credential issuance policy.
- ci: pipeline stages, merge gates, and a merge evaluator that honors per-repo review requirements.

## 0.2.0
- deploy: deployment specs and a canary-then-full rollout controller reporting convergence.

## 0.1.0
- Initial extraction of the control plane: monitor coverage policy and a baseline reconciliation engine.
