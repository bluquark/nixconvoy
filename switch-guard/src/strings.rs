//! Every sentence the crate says: the verdicts, refusals and notes of the
//! plan cores, the switch protocol's messages, the kit's journal lines.
//! nixconvoy's own are in src/nixconvoy/strings.py.
//!
//! `text!(name)` expands to the literal, so `format!(text!(name), field =
//! value)` fills it; every `{field}` is passed by name (a format string
//! made by a macro captures nothing from its scope).  Edit the wording
//! freely; keep each {field}.
#[macro_export]
macro_rules! text {
    // --- plan.rs
    (plan_branches_key) => { "nixconvoy.toml's branch_prefix" };
    (plan_failed) => { "{what} failed" };
    (plan_failed_detail) => { "{what} failed:\n{detail}" };
    (plan_mismatch) => { "nixconvoy-plan asked {q:?} and got {r:?}" };

    // --- plan/drift.rs
    (drift_this_branch) => { "over this checkout's deploy of {when}" };
    (drift_who) => { "{machine}:{checkout} (pid {pid}) at {when}" };
    (drift_who_untimed) => { "{machine}:{checkout} (pid {pid})" };
    (drift_unlisted) => { "runs work of {branches} deployed by {who}, not shaped like {BRANCHES_KEY} here, so no union here contains it" };
    (drift_catch_up) => { "runs pushed work deployed by {who}" };
    (drift_pushed_since) => { "runs {branch}'s increment deployed by {who}, pushed since" };
    (drift_branch_gone) => { "runs {branch}'s increment deployed by {who}, the branch gone since" };
    (drift_no_base) => { "nothing" };
    (drift_in_flight) => { "runs an increment of {branch} deployed by {who} whose push has not landed ({remote}/{branch} is still at {at})" };
    (drift_abandoned) => { "{why}; its deploy is over, the announcement cleared without the push, so taken as abandoned" };
    (drift_looks_dead) => { "{why}; that deploy looks dead, its announcement no longer renewed" };
    (drift_taken_over) => { "{why}; taken over by {EVICT_FLAG}" };
    (drift_not_reached) => { "{why}; this checkout's work does not reach it" };
    (drift_abandoned_increment) => { "{branch}'s abandoned increment {short}" };
    (drift_reverting) => { "{why}; reverting {notes}" };
    (drift_no_toplevel) => { "nixconvoy-plan was given no evaluated toplevel for {target}" };
    (drift_fix_in_flight) => { "re-run once that deploy ends, or push its branch from its checkout" };
    (drift_fix_dead) => { "the deploy of {branch} looks dead (its announcement is no longer renewed): clear it to have the next deploy revert its increment - git push {remote} :{PENDING_REFS}/{key} - or push {branch} from its checkout to keep it" };
    (drift_fix_unlisted) => { "redeploy its work from a branch shaped like {BRANCHES_KEY}, or --evict" };
    (drift_fix_joiner) => { "; or " };
    (drift_refused) => { "nothing switched or committed: {targets} {run} another checkout's work this union lacks, and this checkout's work reaches {them} too (see above).  {fix}; or deploy with {EVICT_FLAG} {first} to revert it deliberately - along with every other branch's work there, which {EVICT_FLAG} leaves out." };
    (drift_line_refused) => { "  {target}: refused: {why}" };
    (drift_line_up_to_date) => { "  {target}: up to date" };
    (drift_line_reverting) => { "  {target}: will switch, reverting it: {why}" };
    (drift_line_left_alone) => { "  {target}: left alone: {why}" };
    (drift_line_switch_kind) => { "  {target}: will switch ({kind}: {why})" };
    (drift_line_switch) => { "  {target}: will switch" };
    (drift_line_excluded) => { "  {target}: differs but excluded by the target filter" };

    // --- plan/rebase.rs
    (rebase_up_to_date) => { "up to date" };
    (rebase_left) => { "detached HEAD left as is: nothing of its own, its commits are already pushed" };
    (rebase_not_on_main) => { "{LEFT}, not on {main}; switch to a branch to deploy" };
    (rebase_unwinding_tmp) => { "unwinding a dead run's tmp commit" };
    (rebase_fast_forwarded) => { "fast-forwarded {branch} onto {up}" };
    (rebase_rebased) => { "rebased {branch} onto {up}" };
    (rebase_op_in_progress) => { "a {op} is in progress; finish or abort it first" };
    (rebase_committing_staged) => { "committing the staged changes aside" };
    (rebase_staging_unstaged) => { "staging the unstaged changes aside" };
    (rebase_committing_unstaged) => { "committing the unstaged changes aside" };
    (rebase_restoring_unstaged) => { "restoring the unstaged changes" };
    (rebase_restoring_staged) => { "restoring the staged changes" };
    (rebase_not_clean) => { "rebasing {branch} onto {up} is not clean, left as it was: {detail}" };
    (rebase_stranded) => { "the detached HEAD carries commits not on {up}; push them (git push {remote} HEAD:{branch}) or put them on a branch, then re-run" };
    (rebase_detached_moved) => { "fast-forwarded the detached HEAD onto {up}" };
    (rebase_detached_stuck) => { "cannot move the detached HEAD onto {up}, left as it was:\n{detail}" };
    (rebase_cannot_check_out) => { "cannot check out {branch} to rebase it:\n{detail}" };
    (rebase_fetch) => { "fetch" };
    (rebase_push_failed) => { "pushing {branch} failed:\n{detail}" };
    (rebase_pushed) => { "pushed {branch}" };
    (rebase_steps_joiner) => { "; " };
    (rebase_setting_upstream) => { "setting {branch}'s upstream" };
    (rebase_other_branch) => { "on branch {other}, which carries commits not on {main_short}; deploys run from {wb} or from {main}" };
    (rebase_switch_failed) => { "cannot switch to {wb} with the current staged/working-tree changes:\n{detail}\nresolve by hand, then re-run" };
    (rebase_switched) => { "switched to work branch {wb}" };

    // --- plan/prune.rs
    (prune_fetch_failed) => { "{remote} could not be fetched to judge it:\n{detail}" };
    (prune_name_locked) => { "its name is locked: a create or another removal is at it; re-run" };
    (prune_deploy_lock_held) => { "a nixconvoy runs in it" };
    (prune_unlocked) => { "unlocked: no session to outlive; `nixconvoy close {name}` judges it" };
    (prune_sessionless) => { "its create found no claude session to outlive: `nixconvoy close {name}` removes it (undeploy --back-to {main_short} first when it holds increments), or `git worktree unlock` it to let prune judge it" };
    (prune_no_reason) => { "no reason given" };
    (prune_hand_lock) => { "locked by hand ({why}): `git worktree unlock` it to let prune judge it" };
    (prune_session_runs) => { "the claude session that spawned its agent still runs" };
    (prune_op_in_progress) => { "a {op} is in progress" };
    (prune_push_in_flight) => { "a push of its work branch is in flight" };
    (prune_nested) => { "another worktree lives inside it" };
    (prune_no_work_branch) => { "it owns no work branch" };
    (prune_foreign_branch) => { "it has {wb}, another checkout's work branch, checked out" };
    (prune_pushed_increments) => { "its work branch {wb} carries pushed increments: land or undeploy decides it" };
    (prune_off_pushed_branch) => { "it is not on {wb}, which carries pushed increments; `git switch {wb}` there to discard them" };
    (prune_unpushed) => { "it owns {wb}, which carries commits not on {remote} (a deploy whose push failed?); push it or `git switch {wb}` there and undeploy --back-to {main_short}" };
    (prune_dirty) => { "it has uncommitted or untracked changes, which the removal would lose; commit or discard them (git status there)" };
    (prune_not_on_origin) => { "it carries commits not on {remote}, which the removal would lose; push them (git push {remote} HEAD:{main}) or deploy from the work branch" };
    (prune_occupied) => { "a process works in it" };
    (prune_remove_refused) => { "git refused to remove it:\n{detail}" };

    // --- plan/union.rs
    (union_unlisted) => { "warning: ignoring {remote}/{name}: not shaped like {BRANCHES_KEY}" };
    (union_merging) => { "union: merging {name}: {summary}" };
    (union_merge_what) => { "merging {name} into the union" };
    (union_dropping_msg_file) => { "dropping {MSG_FILE} from the union" };
    (union_adding) => { "adding {file}" };
    (union_committing_with) => { "committing the union with {name}" };
    (union_tree_failed) => { "writing the union's tree failed" };
    (union_commit_failed) => { "committing the union failed" };
    (union_checking_out) => { "checking out the union" };
    (union_conflict) => { "cannot union work branch {name}; mergiraf could not resolve:\n  {files}\nits last deploy: {summary}\noptions:\n  - fix or rebase that branch in its own checkout, redeploy from there, re-run here\n  - if that work is abandoned: git push --force-with-lease {remote} {main_short}:{name}\n  - --evict deploys ignoring all other branches, reverting their work on every target it switches (each returns there with its own checkout's next deploy)" };
    (union_lane_left) => { "warning: not deploying to {targets}: {why}" };
    (union_lane) => { "union for {targets}, the other branches as deployed there:" };
    (union_lane_conflict) => { "cannot union work branch {name} as {targets} {run} it ({short}); mergiraf could not resolve:\n  {files}\noptions:\n  - deploy its tip there from its own checkout (nixconvoy deploy {args}) or land it, then re-run here\n  - --evict deploys ignoring all other branches, reverting their work on every target it switches (each returns there with its own checkout's next deploy)" };
    (union_fetching) => { "fetching {remote}" };
    (union_origin_moved) => { "{remote} moved since the last fetch; redoing the union" };
    (union_refreshing) => { "bringing the checkout onto {main_short}" };
    (union_creating_worktree) => { "creating the union worktree" };
    (union_waited_too_often) => { "waited for {WAITS} deploys in one run; re-run" };
    (union_redo_after_wait) => { "redoing the union with what that deploy pushed" };
    (union_moved_during_build) => { "work branches moved during the build; rebuilding the union" };
    (union_kept_moving) => { "{remote} kept moving during {ATTEMPTS} attempts; re-run" };

    // --- control.rs
    (control_switch_failed) => { "the build succeeded but the switch of {target} failed; nothing was committed or pushed - the changes are still staged.  Fix the problem and re-run the deploy from this checkout." };
    (control_session_lost) => { "{target}: the activation's ssh session was lost; verified and confirmed" };
    (control_moved) => { "{target} was switched by another deploy while this one ran (now running {cur}).  Nothing was committed; re-run to converge with it." };
    (control_guard_failed) => { "could not take {target}'s switch lock ({reason}); nothing was committed" };
    (control_connection_failed) => { "connection failed" };
    (control_probe_failed) => { "could not root {target}'s switch kit (exit {rc}); nothing was switched or committed" };
    (control_switching) => { "switching {target} ({place})..." };
    (control_switching_confirmed) => { "switching {target} ({place}), confirmed..." };
    (control_no_deadline) => { "{target}'s confirmed-switch helper gave no deadline ({text:?})" };
    (control_arm_failed) => { "could not arm {target}'s rollback timer; nothing was switched or committed" };
    (control_staging) => { "{target}'s new system takes effect only at a reboot; staging it" };
    (control_verifying) => { "verifying {target}..." };
    (control_fail_verify) => { "NIXCONVOY_FAIL_VERIFY=1" };
    (control_staged) => { "the new system of {target} has an init incompatible with the running one, so it takes effect only at a reboot, which a confirmed switch never does.  Its profile and bootloader entry are set: reboot {target}, then re-run.  Nothing was committed or pushed." };
    (control_staging_failed) => { "the new system of {target} has an init incompatible with the running one, so it takes effect only at a reboot, which a confirmed switch never does.  Staging it failed (see above).  Nothing was committed or pushed." };
    (control_runs_other) => { "{target} runs {cur}, not the new system" };
    (control_nothing_reachable) => { "nothing reachable" };
    (control_check_timed_out) => { "{target}: check timed out after {secs:.0}s" };
    (control_check_problem) => { "{target}: {last}" };
    (control_check_failed) => { "check failed" };
    (control_controller_timed_out) => { "this machine's check for {target} timed out after {secs:.0}s" };
    (control_controller_problem) => { "this machine's check for {target}: {last}" };
    (control_did_not_verify) => { "the switch of {target} did not verify ({problem}); rolled back to {previous}.  Nothing was committed or pushed - the changes are still staged." };
    (control_unconfirmed) => { "{target} could not be confirmed; rolling back" };
    (control_confirm_rolled_back) => { "{target} verified but could not be confirmed (see above: its deadline passed first, or persisting the new system failed); rolled back to {previous}.  Nothing was committed or pushed." };
    (control_rollback_failed) => { "the switch of {target} did not verify ({problem}) and rolling it back failed too (see above): it runs {cur}; its timer retries if still pending.  Nothing was committed or pushed." };
    (control_confirm_rollback_failed) => { "{target} verified but could not be confirmed (see above: its deadline passed first, or persisting the new system failed); rolling it back failed too, it runs {cur}.  Nothing was committed or pushed." };
    (control_out_of_time) => { "{target}: no time left to verify before its rollback deadline" };
    (control_verify_failed) => { "{target} failed verification ({problem}); rolling back" };

    // --- switch/core.rs
    (switch_nothing) => { "nothing" };
    (switch_waiting_rollback) => { "{P}: waiting for an earlier rollback to finish" };
    (switch_taking_over) => { "{P}: an unconfirmed switch from {prev} is still pending; taking over its rollback" };
    (switch_no_current_system) => { "{P}: cannot read /run/current-system" };
    (switch_timer_failed) => { "{P}: could not start the rollback timer" };
    (switch_armed) => { "{P}: armed, rolling back to {previous} unless confirmed within {deadline}s of its activation" };
    (switch_rolled_back_not_activating) => { "{P}: already rolled back (the deadline passed first?), not activating" };
    (switch_not_armed) => { "{P}: not armed" };
    (switch_rolled_back_not_confirming) => { "{P}: already rolled back (the deadline passed first?), not confirming" };
    (switch_running_other) => { "{P}: running {cur}, not {want}" };
    (switch_confirmed) => { "{P}: confirmed {new}" };
    (switch_already_confirmed) => { "{P}: already confirmed, nothing to roll back" };
    (switch_already_rolled_back) => { "{P}: already rolled back to {previous}" };
    (switch_rolling_back) => { "{P}: rolling back {cur} to {previous}" };
    (switch_rollback_failed) => { "{P}: rollback failed, still running {cur}" };
    (switch_confirm_failed) => { "{P}: could not confirm {target}, rolling back" };
    (switch_rolled_back_log) => { "rolled back to {previous}" };
    (switch_rolled_back) => { "{P}: rolled back to {previous}" };

    // --- switch/driver.rs
    (switch_cannot_record) => { "{P}: cannot record the state: {e}" };
    (switch_unparsable_state) => { "{P}: {e}; a state file it cannot parse refuses every verb: once no switch is in flight, remove it (sudo rm {path})" };
    (switch_lock_error) => { "lock {path}: {e}" };
    (switch_lock_waiting) => { "{P}: waiting for switch-to-configuration's lock, held by pid {pid} ({comm})" };
    (switch_lock_still_waiting) => { "{P}: still waiting for pid {pid} ({comm}), {secs:.0}s" };
    (switch_lock_unseen) => { "{P}: switch-to-configuration's lock is held, by no live process /proc/locks identifies; rolling back anyway" };
    (switch_lock_released) => { "{P}: switch-to-configuration's lock released after {secs:.1}s" };
    (switch_usage) => { "usage: nixconvoy-confirmed-switch arm | activate <toplevel> | confirm <toplevel> [reboot-pending] | rollback | check | deadline" };
    (switch_check_not_root) => { "check must run as root" };
    (switch_check_no_internet) => { "cannot reach the internet" };
    (switch_check_cannot_run) => { "cannot run {checks}: {e}" };
    (switch_bad_config) => { "{P}: {e}" };

    // --- boot/driver.rs
    (boot_target_missing) => { "boot-assessment: {target} does not exist" };
    (boot_bad_record) => { "boot-assessment: {e}; not sparing a pending rollback's system - remove that file once no confirmed switch is in flight" };
    (boot_esp_full_notice) => { "{host}'s ESP lacks {mib} MiB for the generations its failover needs (target, booted, running, previous): every switch fails to install its boot entry until space is made on {esp}.\n" };
    (boot_esp_full) => { "boot-assessment: the ESP lacks {bytes} bytes even for the generations the failover needs" };
    (boot_prune_no_profile) => { "boot-assessment: {profile} does not exist, not pruning" };
    (boot_prune_no_esp) => { "boot-assessment: cannot read {dir}, not pruning: {e}" };
    (boot_pruning) => { "boot-assessment: pruning generations {drop} (keeping {kept}: the bootable set, the stubbed, anchors {anchors}, and the newest {newest})" };
    (boot_would_prune) => { "boot-assessment: would prune generations {drop} (keeping {kept}: the bootable set, the stubbed, anchors {anchors}, and the newest {newest})" };
    (boot_none) => { "none" };
    (boot_unknown) => { "?" };
    (boot_log_failed) => { "boot-assessment: {path}: {e}" };
    (boot_failover_notice) => { "{host} booted generation {bgen} at {date}, not the newest ({ngen}).\nRejected entries on the ESP: {rejected}\nnixconvoy refuses to switch back onto a rejected generation: change the configuration (a new generation gets its own tries), or re-arm the stub to retry it as is (sudo mv of the +0-N stub to +{tries}, then deploy).\nThe boot-assessment notice, if set, says why the newest one failed.\nLog: journalctl -u boot-assessment-watch -u boot-assessment\n" };
    (boot_checks_exited) => { "the boot-assessment checks exited {code}" };
    (boot_checks_cannot_run) => { "cannot run the checks: {e}" };
    (boot_switch_checks_failed) => { "switch checks failed" };
    (boot_switch_checks_cannot_run) => { "cannot run the switch checks: {e}" };
    (boot_gate_passed) => { "reachable: sshd, tailscaled, the boot-assessment checks pass" };
    (boot_gate_deadline) => { "not reachable by the {deadline}s deadline: {status}" };
    (boot_gate_settled) => { "not reachable with nothing left starting, {t}s into stage 2: {status}" };
    (boot_already_blessed) => { "{entry} already blessed this boot" };
    (boot_no_watchdog) => { "hardware watchdog not armed" };
    (boot_assessing) => { "assessing {entry}: watchdog {watchdog}s, panic reboot, deadline {deadline}s" };
    (boot_blessed) => { "passed: {entry} is blessed" };
    (boot_bless_failed_notice) => { "{host}'s boot of {entry} passed its assessment at {date} but systemd-bless-boot failed, hangs or never ran: the stub keeps its tries counter, and each reboot spends one until the ESP takes the rename.\nLog: journalctl -u systemd-bless-boot\n" };
    (boot_not_blessed) => { "reachable, but not blessed: systemd-bless-boot failed or never ran" };
    (boot_gate_never_ran) => { "the gate never ran" };
    (boot_failed_notice) => { "{host}'s boot of {entry} failed its assessment at {date}: {status}\nRebooting: systemd-boot retries the entry until its tries are spent, then boots the generation deployed before it (if it was never booted), then the last blessed one.\nLog: journalctl -u boot-assessment-watch -u boot-assessment\n" };
    (boot_usage) => { "usage: nixconvoy-boot-assessment --config <file> install <toplevel> | prune [--dry-run] | report | gate | watch" };
    (boot_bad_config) => { "boot-assessment: {e}" };

    // --- boot/plan.rs
    (boot_dropping) => { "boot-assessment: dropping never-proven generation {g} ({stub})" };
    (boot_drop_failed) => { "boot-assessment: could not drop generation {g}, leaving it" };
    (boot_not_fit) => { "boot-assessment: generation {g} would not fit the ESP, leaving it out" };
    (boot_installing) => { "boot-assessment: installing generations {chosen} (anchors: {anchors})" };

    // --- config.rs
    (config_no_equals) => { "config line {n}: no '='" };
    (config_cannot_read) => { "cannot read {path}: {e}" };
    (config_missing) => { "config lacks {key}" };
    (config_not_a_number) => { "config {key} is not a number" };
    (config_word_not_a_number) => { "config {key}: {w} is not a number" };

    // --- record.rs
    (record_bad_magic) => { "not a confirmed-switch state file" };
    (record_unexpected_line) => { "unexpected line {line:?}" };
    (record_no_previous) => { "no previous" };
    (record_bad_phase) => { "bad phase" };
    (record_path_error) => { "{path}: {e}" };

    // --- wire.rs
    (wire_failed) => { "{binary}: {msg}" };
    (wire_no_parameters) => { "no parameters" };
}
