# meridian-jobsvc

The Meridian cluster and job-orchestration service. `JobService` keeps every job and gets it run as a process
on a node of the cluster's fleet: a dependency-aware priority queue orders the waiting jobs, each starts on the
first free node with the cpu and memory it asks for, and the daemon on that node runs it, one job per node,
under an identity of its own: never its submitter's. It serves an HTTP API and MCP tools (`meridian_jobsvc.api`)
under the company's central authorization.

The package also holds a capacity-based scheduler with an autoscaler simulation, a worker runtime that hosts
long-lived jobs (heartbeat and checkpoint), node lifecycle (cordon, drain, rebalance), a spend/quota ledger,
and a compact job-spec DSL.

Depends on: meridian-common.

    pytest
