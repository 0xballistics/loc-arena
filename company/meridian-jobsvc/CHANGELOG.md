# Changelog — meridian-jobsvc

## 0.6.0
- service: each job runs under an identity of its own, which the central authority issues it from its
  submitter's credential before it is queued (`authority.WorkloadIssuer`); its process gets each of its secrets
  in `MERIDIAN_<SERVICE>_TOKEN`, and the service keeps none once the node has it. A job shows its `principal`.
- api: a job the authority refuses an identity is a 403, and one it cannot answer for a 503; nothing is queued.

## 0.5.0
- service: `JobService` runs each job as a process on a node of the fleet, one job per node, through each
  node's daemon; a job's state follows its process, and a cancelled job is `cancelling` until it is gone.
- api: the HTTP API and MCP tools `submit_job`, `list_jobs`, `get_job`, `job_logs`, `cancel_job`, decided by
  the central authority; only a job's owner cancels it.

## 0.4.0
- lifecycle: node cordon, drain (checkpoint-preserving eviction), and rebalance.
- accountant: per-identity spend + quota ledger with a report.

## 0.3.0
- worker: WorkerRuntime hosting long-lived jobs with heartbeat and checkpointing.
- dsl: a compact job-spec DSL parsed into a common JobSpec.

## 0.2.0
- scheduler: capacity-based placement plus an autoscaler simulation over a target band.

## 0.1.0
- Initial extraction of the cluster service: dependency-aware priority job queue and the JobTransport service.
