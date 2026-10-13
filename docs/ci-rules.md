# Pull request and shipping checks

Every pull request runs the existing Ruff/format/typing/test gates and a separate fast file-size job. The
700-line limit applies to tracked UTF-8 text, including tests and Markdown. Existing oversized files have
fixed non-growing ceilings in `scripts/file_size_limits.json`; remove a ceiling when that file is split below
700 lines. Binary files, symlinks and generated `uv.lock` are excluded. Private ignored files are never scanned.
Run `python3 scripts/check_file_size.py` locally; `make gates` runs it too.

## CodeRabbit must return a clean latest-commit review

The `CodeRabbit clean review` check requires a successful CodeRabbit status, a CodeRabbit review associated
with the current PR head commit, zero actionable comments in that head's reviews, no new inline findings on
that head, and no unresolved older CodeRabbit review threads. A successful CodeRabbit status alone is not
sufficient: CodeRabbit can finish successfully while reporting findings. Blank completed review bodies with
no inline findings are accepted; positive reported findings fail even if a reply marks them resolved.

Fix verified findings, explain any rejected finding on its thread, resolve addressed threads, then obtain a
fresh CodeRabbit review on the new head. Do not disable the bot, forge a review or edit its finding count to
pass. This check does not assess the correctness of a fix or cover non-inline nitpicks in summary issue comments;
normal review remains necessary. Human review requirements are separate.

The gate refreshes on PR opening/new commits and CodeRabbit status updates. Review-event triggers are
excluded because their workflow YAML can come from the PR merge ref, before any trusted checkout occurs.
After resolving a thread without a new commit/status event, manually run the workflow with the PR number.
GitHub does not provide a review-thread-resolution trigger to this workflow. Pending, unavailable or stale
review metadata fails closed. The workflow and scripts execute only from the trusted default branch; no
pull-request code runs with its check-writing token. It posts a check directly on the current PR head SHA.
It becomes active after the workflow lands on the default branch.

## Maintainer setup required before this blocks shipping

A repository administrator must configure main's branch protection/ruleset to require:

- `file size (700 lines)`
- `ruff, ty, pytest`
- `CodeRabbit clean review`
- Conversation resolution for all review threads, and no bypass if the policy is truly universal.

Use equivalent requirements on any shipping/release branch. The current automation account has push access
but no admin/maintain access, so these settings have not been changed. Without required checks, CI reports
failures but cannot prevent a maintainer from merging. Confirm bot automatic reviews cover the repository;
when CodeRabbit pauses reviews, explicitly request another review rather than bypassing the fresh-review gate.
