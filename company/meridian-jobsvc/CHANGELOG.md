# Changelog — meridian-jobsvc

## 0.7.0
- service: a waiting job can be held and released; a drained node starts no new job, and once its grace is
  over its job runs again elsewhere as its next attempt, under the same id and identity, which the service
  keeps until the job ends. Each team is charged the CPU-hours its jobs ran, from their wall time, a job's team
  coming from the cluster's own seed, and the cluster's policy caps a team's running jobs and CPU-hours and the
  priority a job may ask. A deployment may refuse a submitter of its own accord.
- schedules: a schedule submits its job on a cron expression or an interval, every run under the one identity
  issued when it was created; a due time that finds the last run unfinished is skipped.
- api: `hold_job`, `release_job`, `create_schedule`, `list_schedules`, `get_schedule`, `delete_schedule`,
  `list_nodes` and `usage`, and the operators' `operate_job`, `operate_schedule` and `drain_node` (`operate`).
  A job may carry `environment`, variables for its process. An unknown schedule or node is a 404.
- accountant: the ledger keeps each account's spend, and the service charges each team's.

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
