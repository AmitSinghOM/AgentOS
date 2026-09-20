# Fail modes — which way each chokepoint fails, and the test that pins it

Every place AgentOS can refuse, stop, or carry on under a fault is listed here with its
direction. **Fail-closed** means the fault stops the action and nothing unverified is
recorded or executed. **Fail-open** means the fault is absorbed and the run continues; the
right to fail open is earned only where the affected component is *derived* from the log
(telemetry, snapshots, resolvers) and can be rebuilt. Verification tools add three honest
outcomes that are neither: **reported** (a condition the tool names loudly but cannot call
wrong — deleted seals, an unknown key), **warn** (unconfigured, not broken — asserted auth, no
policy, no keyring) and **not detectable in-log** (the one gap no in-log scheme covers,
truncation after the last seal; the tool reports the uncovered tail). A chokepoint in none
of these is a bug.

Each row names the test that pins the direction (61 rows). `tests/test_fail_modes.py` checks that
every cited test exists, so this table cannot quietly outlive the code. Phase 8 #11;
the operator policy ceiling (#2) is scoped against this table.

## Execution (worker)

| Chokepoint | Fault | Direction | What happens | Pinned by |
| --- | --- | --- | --- | --- |
| Gate, before dispatch | Step declares an effect outside the run's budget | **closed** | Refused before the executor is called; `step.dead_lettered` names the class | `tests/test_governor.py::test_declared_effect_outside_budget_is_refused_before_dispatch` |
| Gate, before dispatch | Step declares `spend` / `write_external` (tier 2) | **closed** | `approval.requested`, run `suspended`; nothing runs until a principal decides | `tests/test_approvals.py::test_approve_resumes_and_step_runs_exactly_once_after_the_grant` |
| Gate, tier 3 vs tier 2 | A refusal and an approval request in the same wave | **closed** | Refusal wins; no approval is asked for a step that could never run | `tests/test_approvals.py::test_tier3_refusal_wins_over_tier2_request` |
| Settle, after the step | Executor reports an effect it did not declare | **closed** | Dead-lettered with the class named, even though the effect happened; the run fails | `tests/test_governor.py::test_reported_undeclared_effect_is_dead_lettered_with_the_class_named` |
| Settle | Step cost over its budget / run over its ceiling | **closed** | Recorded first (the money is spent), then dead-lettered / run suspended | `tests/test_governor.py::test_step_cost_over_budget_is_dead_lettered_and_run_ceiling_suspends_after_recording` |
| Settle | Step over `max_step_wall_seconds` | **closed** | Dead-lettered | `tests/test_governor.py::test_wall_time_over_budget_is_dead_lettered` |
| Settle | Executor returns something that is not a `StepResult` | **closed** | Dead-lettered, not a crash | `tests/test_governor.py::test_non_stepresult_return_is_dead_lettered_not_crash` |
| Settle | Executor raises | **closed, bounded** | `step.failed` with backoff; the last attempt dead-letters with the cause | `tests/test_scheduler.py::test_poison_step_dead_letters_after_last_attempt_with_cause` |
| Settle, wave mate | One step in a wave dead-letters | **closed for the run, open for siblings** | The completed sibling's event is recorded, never lost; the run fails | `tests/test_scheduler.py::test_completed_sibling_is_never_lost_when_wave_mate_dead_letters` |
| Definition resolution | Run's pinned agent version no longer exists | **closed** | `run.failed` in the log; never silently resolves to latest | `tests/test_agent_versions.py::test_pinned_version_missing_fails_in_the_log_not_silently_upgrades` |
| Definition resolution | Workflow changed between crash and resume | **closed** | Refused; a run never resumes against a definition it did not start with (C3) | `tests/chaos/test_fault_points.py::test_definition_changed_between_crash_and_resume_is_refused` |
| Definition resolution | Named executor plugin not installed | **closed** | Run fails with an install hint in the log | `tests/test_plugins.py::test_missing_named_executor_fails_the_run_with_install_hint` |
| Executor `resolve()` hook | Plugin's resolver raises | **open** | Treated as "cannot resolve now"; the step still dispatches and the executor reports the real error at its own boundary | `tests/test_plugins.py::test_resolver_that_raises_does_not_stop_the_engine` |
| Plugin discovery | One entry point fails to import | **open** | Skipped and logged; the others load | `tests/test_plugins.py::test_discover_executors_loads_good_skips_broken_and_dedupes` |

## Durability (store, lease, process)

| Chokepoint | Fault | Direction | What happens | Pinned by |
| --- | --- | --- | --- | --- |
| Crash before effect commit | Worker dies after the step ran, before `step.completed` | **closed** | Step re-runs on redelivery; exactly one completion is recorded | `tests/chaos/test_fault_points.py::test_crash_before_effect_commit_reruns_step_but_records_one_completion` |
| Crash after effect commit | Worker dies after `step.completed`, before the run advances | **closed** | Replay skips the step; it does not execute twice | `tests/chaos/test_fault_points.py::test_crash_after_effect_commit_replays_without_reexecuting` |
| Crash after run commit, before ack | Queue redelivers a finished run | **closed** | No-op | `tests/chaos/test_fault_points.py::test_crash_after_run_commit_before_ack_is_a_noop_on_redelivery` |
| Real process kill | `kill -9` mid-run, restart | **closed** | Finishes without repeating the completed step | `tests/chaos/test_kill9_real_process.py::test_kill_9_after_step_2_then_restart_finishes_without_repeating_step_2` |
| Two workers, one run | Both hold the run | **closed** | Exactly one advancement per step (fence + expected seq) | `tests/chaos/test_fault_points.py::test_two_workers_one_run_exactly_one_advancement_per_step` |
| Lease lost mid-step | `heartbeat()` returns False during `progress()` | **closed** | Stops without writing a completion; the new holder finishes | `tests/test_governor.py::test_lease_lost_during_progress_stops_without_writing_completion` |
| Lease expiry under network fault | Stale worker writes after takeover (Toxiproxy) | **closed** | Fenced at the store; its write is rejected, not merged | `tests/chaos/network/test_lease_expiry_race.py::test_lease_expiry_race_stale_worker_is_fenced_not_merged` |
| Optimistic append | A foreign non-control event appeared since the last write | **closed** | `ConflictError`; the advance stops | `tests/test_control.py::test_foreign_non_control_append_is_still_a_conflict` |
| Optimistic append | A control request (cancel / pause) appeared since the last write | **open, narrowly** | Adopted — the only foreign appends the engine accepts | `tests/test_control.py::test_cancel_request_appended_by_api_is_adopted_by_worker_log_not_a_conflict` |
| `_fail` under conflict | Two settles fail the run at once | **open** | Second `run.failed` is dropped; the log already says failed. **Unpinned**: no test drives two concurrent failures into `_fail`; listed so the gap is visible | `tests/test_fail_modes.py::test_unpinned_rows_are_named_here` |
| Worker loop | A transient store error (connection reset, `database is locked`) or an escaped executor bug while handling one delivery | **closed** | Logged with the run id and traceback; the loop backs off and continues; the un-acked delivery is redelivered after the visibility timeout. `run_once` still raises | `tests/test_worker_resilience.py::test_run_forever_survives_a_transient_store_error_and_finishes_the_run` |
| API restart | Process restarts mid-run | **closed** | Run and events are on disk; paging by seq continues | `tests/test_api_durability.py::test_run_survives_app_restart_and_events_page_by_seq` |
| Schema migration | Released migration edited | **closed** | Hash-pinned; the test fails with "add migration N+1" | `tests/test_migrations.py::test_released_migrations_are_never_edited` |

## Derived state (may fail open — it is rebuilt from the log)

| Chokepoint | Fault | Direction | What happens | Pinned by |
| --- | --- | --- | --- | --- |
| Snapshot write | Store's `put_snapshot` raises | **open** | Logged, the advance succeeds, nothing is cached, the log still folds. (That a *prior* exception is not replaced is by construction — the write is wrapped in its own `try` inside `finally` — and not separately tested.) | `tests/test_store_typed_and_bounded.py::test_a_failing_snapshot_write_never_fails_the_advance` |
| Snapshot read | Snapshot does not anchor to the log (tampered, stale, beyond the tail) | **closed for the snapshot, open for the read** | Ignored; the full log is folded | `tests/test_store_typed_and_bounded.py::test_a_snapshot_that_does_not_chain_to_the_log_is_ignored_not_trusted` |
| Snapshot write ordering | Stale worker writes an older snapshot | **closed** | Monotonic per run; the older one is dropped | `tests/test_store_typed_and_bounded.py::test_put_snapshot_is_monotonic_per_run` |
| Observer | An observer raises | **open** | Logged, swallowed; telemetry is derived and rebuildable | `tests/test_observability.py::test_observer_failure_never_breaks_the_engine` |
| Log integrity on read | Hash chain broken | **closed** | Does not fold; the API returns 500 and says so | `tests/test_trust_boundary.py::test_a_tampered_log_does_not_fold_and_the_api_says_so` |
| Seal, rewrite-and-rechain | Prefix rewritten and every hash recomputed | **closed** | The seal after the edit points at a hash the log no longer carries → `INVALID`; `agentos verify` exits 1 | `tests/test_seal_and_cli.py::test_rewrite_and_rechain_is_caught_by_the_seal` |
| Seal, forged signature | Seal over the right hash with a wrong signature | **closed** | `INVALID`, names the seal and key | `tests/test_seal_and_cli.py::test_forged_seal_over_the_right_hash_fails_the_signature` |
| Seal, deleted | All seals removed and the log rechained | **reported** | State `unsigned`, `unsigned_tail` = whole log; not silently fine | `tests/test_seal_and_cli.py::test_deleting_the_seals_is_visible_as_an_unsigned_tail` |
| Seal, unknown key | Seal signed by a key this keyring does not hold | **reported** | `unverifiable`, key id listed; never treated as valid | `tests/test_seal_and_cli.py::test_unknown_key_is_reported_and_a_foreign_keyring_is_unverifiable` |
| Seal, truncation after last seal | Events after the last seal deleted | **not detectable in-log** | `unsigned_tail` reports how many events are uncovered; an external anchor is the only fix (not built) | `tests/test_seal_and_cli.py::test_no_keyring_means_no_seals_and_identical_logs` |
| Signing startup | `AGENTOS_SIGNING_KEYS` set but missing / short key / bad active | **closed** | Process refuses to start naming the entry | `tests/test_seal_and_cli.py::test_keyring_from_env_warns_when_unset_and_fails_closed_when_bad` |
| Signing unset | — | **not a boundary** | Chains hash-linked but unsigned; one WARNING | `tests/test_seal_and_cli.py::test_no_keyring_means_no_seals_and_identical_logs` |

## Boundary (API)

| Chokepoint | Fault | Direction | What happens | Pinned by |
| --- | --- | --- | --- | --- |
| Request body | Body larger than `AGENTOS_MAX_BODY_BYTES` (default 1 MiB), or a bodyful request with no `Content-Length` | **closed** | 413 / 411 before any of the body is read; nothing parsed or stored | `tests/test_body_limit.py::test_oversized_declared_body_is_refused_before_parsing` |
| Control payload | Body carries anything but `principal` + `reason` | **closed** | 422 before the engine, logged with the field names; nothing appended | `tests/test_trust_boundary.py::test_resume_payload_with_unexpected_fields_is_rejected_logged_and_starts_nothing` |
| Authentication (bearer mode) | No / unknown token | **closed** | 401 on every path except `/health`, `/metrics`; logged by hash prefix | `tests/test_auth.py::test_anonymous_caller_is_401_everywhere_except_probes` |
| Authentication (bearer mode) | Body carries a `principal` | **closed** | 422 — rejected, not replaced | `tests/test_auth.py::test_recorded_principal_comes_from_the_token_not_the_body` |
| Authentication (bearer mode) | Route added without a `Depends` | **closed** | Middleware covers it | `tests/test_auth.py::test_every_route_is_covered_by_the_middleware_not_a_dependency` |
| Authentication startup | `bearer` with no token file / plaintext token in the file | **closed** | Process refuses to start, naming the variable or entry and the fix | `tests/test_auth.py::test_bearer_without_a_token_file_fails_startup_naming_the_variable` |
| Authentication (asserted mode) | — | **not a boundary** | Body principal recorded unverified; one WARNING at startup | `tests/test_auth.py::test_default_mode_is_asserted_and_warns_once` |
| Authorization | Non-human approves `spend` / `write_external` | **closed** | 403 from the engine; nothing appended | `tests/test_approvals.py::test_spend_requires_human_unless_workflow_allows_agent_approval` |
| Authorization | `agent`-kind token registers an agent / defines a workflow | **closed** | 403 before the store | `tests/test_auth.py::test_agent_token_cannot_register_agents_or_define_workflows` |
| Operator policy, gate | Workflow allows a class outside `effect_ceiling` | **closed** | Refused before dispatch; `governance.policy_applied` records the narrowing | `tests/test_policy.py::test_free_spend_outside_the_ceiling_is_refused_before_dispatch_and_audited` |
| Operator policy, approve path | Workflow sets `allow_agent_approval`, policy forbids it | **closed** | Agent's approve is refused; the check reads the effective budget, not the workflow's | `tests/test_policy.py::test_policy_revokes_agent_approval_on_the_approve_path` |
| Operator policy, dispatch | Agent names an executor outside `allowed_executors` | **closed** | `run.failed` naming the policy; executor never called | `tests/test_policy.py::test_executor_outside_the_allowlist_fails_the_run_at_dispatch_without_calling_it` |
| Operator policy, gate ordering | A `spend` step on a forbidden executor | **closed, before asking** | Refused at dispatch; no approval is requested for a step that can never run | `tests/test_policy.py::test_no_approval_is_asked_for_a_step_the_policy_can_never_run` |
| Operator policy, startup | `AGENTOS_POLICY` set but missing / malformed | **closed** | Process refuses to start naming the variable and entry | `tests/test_policy.py::test_load_policy_errors_name_the_variable_and_entry` |
| Operator policy, unset | — | **not a boundary** | No ceiling; one WARNING at startup | `tests/test_policy.py::test_policy_from_env_warns_when_unset_and_loads_when_set` |
| Approval expiry | Nobody decides within the window | **closed** | Rejected by `system` on sweep; run fails | `tests/test_approvals.py::test_expired_approval_is_rejected_by_system_on_sweep` |
| Approval on wrong state | Decide an approval that is not pending | **closed** | 409 | `tests/test_approvals.py::test_decisions_on_wrong_state_are_refused` |
| Stream | Client never disconnects | **closed, bounded** | Connection closes at `AGENTOS_STREAM_MAX_SECONDS`; resume with `Last-Event-ID` | `tests/test_stream.py::test_stream_is_bounded_by_max_seconds_and_sends_keepalives_while_idle` |
| Subprocess tool | Command exceeds its timeout | **closed** | The whole process group is killed | `tests/test_tool_agent.py::test_subprocess_timeout_kills_the_whole_process_group` |
| Subprocess tool | Output over cap | **closed** | Refused | `tests/test_tool_agent.py::test_subprocess_stdout_over_cap_is_refused` |
| HTTP tool egress | Metadata / private address | **closed** | Refused unless opted in | `tests/test_tool_agent.py::test_egress_guard_refuses_metadata_and_private_addresses_unless_opted_in` |

## Deliberately fail-open, and why that is acceptable

Snapshots, observers, plugin resolvers and plugin discovery. Each is *derived*: deleting a
snapshot costs one full fold; telemetry is rebuilt from the log; a resolver that cannot
answer defers to the executor's own error; a broken plugin is not the engine's fault. The
test in each row proves the fault is absorbed **and** that the source of truth (the event
log) is untouched by it.

## Operability (`agentos doctor` / `verify`)

| Chokepoint | Fault | Direction | What happens | Pinned by |
| --- | --- | --- | --- | --- |
| `agentos verify` | Any run's chain or seal fails | **closed** | Exit 1, the run named with the reason | `tests/test_seal_and_cli.py::test_verify_passes_on_a_good_store_and_fails_on_a_tampered_run` |
| `agentos doctor` | Store unreachable | **closed** | Exit 1 on the first check; nothing else attempted | `tests/test_seal_and_cli.py::test_doctor_fails_when_the_store_is_unreachable` |
| `agentos doctor` | Auth asserted / policy unset / keyring unset | **warn** | Reported as warnings, exit 0 — unconfigured is not broken | `tests/test_seal_and_cli.py::test_doctor_reports_configuration_and_every_run` |
