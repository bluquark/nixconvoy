# Nixconvoy

<!-- Github About: A NixOS deployment tool designed for multitasking -->

Nixconvoy is a NixOS deployment tool with a simple command-line interface.  Its distinguishing features are:

* Nixconvoy tracks the history of every change ever deployed to your NixOS fleet --- allowing you to easily reconstruct what happened and revert to an exact previous state.   It does this largely behind the scenes using its own git branches, so your main git branch stays tidy.
* Nixconvoy is designed for quick, iterative experiments on live machines.  If your change breaks ssh connectivity (or any other health check of your choice), then nixconvoy automatically rolls back to the previous NixOS generation.
* Nixconvoy enables multitasking.  It deploys the merge of every worktree’s work-in-progress, so one half-finished change never undoes another.  It also has been tested to work correctly when two or more nixconvoy instances are running simultaneously on the same machines.

Overall, nixconvoy is well-suited to NixOS fleets managed primarily by a single person, with a rapid pace of changes.  It especially shines for vibe-coded NixOS configurations: it effectively provides each agent its own lane, allowing them to iterate side by side on the same machine, without unforced conflicts.

# Getting started

## Requirements

- Control machine (runs the `nixconvoy` process):
	- Linux, WSL, or macOS.
	- Must have Nix with flakes enabled, git 2.35+ and OpenSSH.
	- Contains a git repository containing your NixOS configuration:
		- Flake-based.
		- Must be synced to a git remote such as GitHub.
- Build machine (runs heavy compilation workload):
	- Linux or WSL.
	- Must accept public-key ssh connections from control machine (passwordless).
- Target machines:
	- NixOS only.
	- Must have the same CPU architecture as the builder.
	- Must accept public-key ssh from control and build machines, with passwordless sudo

> [!NOTE]
> You only need one physical machine to use nixconvoy!  Control, build and target can be one and the same.  Nixconvoy will simply use ssh over loopback.

## Installation

There are three ways to install nixconvoy.  Do any one of the following on your control machine:

<details>
<summary><b>1. Quick no-config use</b></summary>

```bash
nix profile install github:bluquark/nixconvoy
alias nixconvoy='nix run github:bluquark/nixconvoy -- '
```

</details>

<details>
<summary><b>2. Pinned flake</b></summary>

1. At the top level of your flake:
   ```nix
   nixConfig = {
     extra-substituters = [ "https://nixconvoy.cachix.org" ];
     extra-trusted-public-keys = [ "nixconvoy.cachix.org-1:8HCQ3qVkg+lUbouy8s7aWzWoDoGDrm0lkg07VvD8RwQ=" ];
   };
   inputs.nixconvoy.url = "github:bluquark/nixconvoy";
   inputs.nixconvoy.inputs.nixpkgs.follows = "nixpkgs";
   ```
2. In outputs:
   ```nix
   packages.x86_64-linux.nixconvoy = nixconvoy.packages.x86_64-linux.nixconvoy;
   ```
3. Add this small wrapper script called `nixconvoy` somewhere on your PATH:
   ```sh
   #!/bin/sh
   exec nix run "$(git rev-parse --show-toplevel)/nixos#nixconvoy" -- "$@"
   ```

</details>

<details>
<summary><b>3. NixOS system package</b></summary>

```nix
nix.settings = {
  substituters = [ "https://nixconvoy.cachix.org" ];
  trusted-public-keys = [ "nixconvoy.cachix.org-1:8HCQ3qVkg+lUbouy8s7aWzWoDoGDrm0lkg07VvD8RwQ=" ];
};
{ nixconvoy, pkgs, ... }: {
  environment.systemPackages = [ nixconvoy.packages.${pkgs.stdenv.hostPlatform.system}.nixconvoy ];
  environment.pathsToLink = [ "/share/nixconvoy" ];
}
```

</details>

Then, `cd` into your NixOS checkout and run **`nixconvoy init`**.  Review the `nixconvoy.toml` config file it generated, and commit it to your main branch.  When it represents your setup correctly, then you are ready to go.

<details>
<summary><b>Bash PS1 helper (optional)</b></summary>
<br>
Nixconvoy includes `nixconvoy/prompt.bash` defining `__nixconvoy_ps1`, a helper to display nixconvoy worktree status on your prompt.  Depending on install method, it is usually in `/run/current-system/sw/share/nixconvoy/prompt.bash` or .  In your `.bashrc`, write e.g.:

```bash
source /run/current-system/sw/share/nixconvoy/prompt.bash
PS1='\h:$(__nixconvoy_ps1)\w\$ '  # simple example; adapt your existing PS1 instead
```

</details>

<details>
<summary><b>Claude code plugin (optional)</b></summary>
<br>
For the plugin to function, both `nixconvoy` and `jq` must be on the `PATH`.  Then:

```bash
claude plugin marketplace add bluquark/nixconvoy
claude plugin install nixconvoy@nixconvoy --scope project
```

This will cause subagents to spawn inside a nixconvoy-managed child worktree.  It has no effect on normal top-level agents.

With the plugin, you can fire off a sweeping prompt like:

```markdown
Audit all of my miniPC's systemd units for security hardening opportunities.
Then divide them into 10 nonconflicting groups and launch one subagent to harden each.
```

The 10 subagents will go off and work for a while, and as long as they don't touch literally the same lines of config, they should be able to `nixconvoy deploy` their respective changes to verify they didn't break anything on the real machine before terminating.  Then, from the parent worktree, you can examine their config changes as a whole with `nixconvoy review --with-children`, and then land all of it as one single commit on your main branch with `nixconvoy land --with-children`.

> **Warning:** Claude Code doesn't guarantee a stable API for plugins, so this plugin is the most likely part of nixconvoy to break over time.  It may also conflict with other Claude Code hooks or plugins if you have any installed.  So I only recommend it for people who heavily use subagents to write code (not just to research or codereview).

</details>


# Tutorial

To begin with a concrete example, let's say you are maintaining a nascent NixOS homelab with one workstation and one headless mini-PC.  Your source of truth is a private repo called "homelab" on GitHub, and you work from a persistent local clone of it called `~/homelab` on your workstation.

Now, let's assume you have two features you want to work on today for your mini-PC: 1) tighten your firewall security to protect your machine against a recent CVE, and 2) install Jellyfin.

## Basic sequential use

Nixconvoy is built around two fundamental commands, `nixconvoy deploy` and `nixconvoy land`.  Both of these operate on one git feature branch or worktree.  `deploy` applies the currently staged changes to your NixOS fleet.  `land` rolls up all changes that have been applied since the last `land` into one git commit, which is pushed to both `main` and `origin/main`.

If you want, you can start by ignoring all of nixconvoy's more advanced capabilities, and simply use it like `nixos-rebuild`.  With this traditional sequential workflow, here's what it might look like to code and test the two changes in feature branches of `~/homelab`:

```console
$ cd ~/homelab/nixos/hosts/minipc/
$ git checkout -b nixconvoy/firewall
$ vim nftables.nix
$ git add -A
$ nixconvoy deploy
$ nixconvoy land -m "Tighten firewall"

$ # First feature complete: begin second feature
$ git checkout main  
$ git checkout -b nixconvoy/jellyfin
$ vim configuration.nix jellyfin.nix
$ git add -A
$ nixconvoy deploy
$ vim jellyfin.nix  # it installed but needs more configuration tweaks
$ # git add -A
$ nixconvoy deploy
$ nixconvoy land -m "Add Jellyfin"
```

## Basic parallel use

Where things get interesting is when you spin up more than one worktree.  As a reminder, a git worktree is a directory containing another checkout of a local git repo, linked to it in a parent/child relationship.

You can spin up one worktree per task: 

```console
$ cd ~/homelab/
$ nixconvoy open ~/homelab-firewall
$ nixconvoy open ~/homelab-jellyfin
```

(`nixconvoy open` creates a new work branch intended to be used exclusively by nixconvoy, then creates a new worktree that checks it out using `git worktree add`.)

With this approach, you can safely work on both features at the same time:

```console
$ cd ~/homelab-firewall/nixos/hosts/minipc/
$ vim nftables.nix
$ git add -A
$ nixconvoy deploy

$ # Hmm, not quite done, I need to research this more, let's work on jellyfin for now

$ cd ~/homelab-jellyfin/nixos/hosts/minipc/
$ vim -o configuration.nix jellyfin.nix
$ git add -A
$ nixconvoy deploy  # !! this affects the same machine as the unfinished firewall change !!
$ nixconvoy land -m "Add Jellyfin"
$ cd ~

$ # Back to half-finished original task...
$ cd ~/homelab-firewall/nixos/hosts/minipc/
$ vim nftables.nix  # add the missing bugfix
$ git add -A
$ nixconvoy deploy
$ nixconvoy land -m "Tighten firewall"

$ # Worktrees are now synced to origin/main, it's safe to remove them
$ cd ~
$ nixconvoy close ~/homelab-firewall
$ nixconvoy close ~/homelab-jellyfin
```

This is just an interleaved version of the sequential flow above... but notice the second deploy I highlighted with exclamation marks.  `~/homelab-jellyfin/` doesn't have the changes in `nftables.nix`, so if this were plain `nixos-rebuild`, the first round of firewall changes would accidentally get removed from the running system!

This is the property of NixOS that can make it tricky to multitask on without switching to a full GitOps-style pipeline (where even the smallest change is required to be pushed to GitHub before getting deployed to a machine).  But with nixconvoy, the problem "magically" doesn't happen!

> [!NOTE]
> How?  nixconvoy actually *does* have a full GitOps-style pipeline in its implementation details: it merely presents itself as a simple push-based deploy tool.  It uses that to remember everything you previously deployed in other worktrees, and merge those changes together in /tmp before applying your latest change.  Nixconvoy uses a syntax-aware merge tool called `mergiraf`, so this process is mostly transparent except when two worktrees changed the exact same line of config.

## Parallel use with coding agents

Parallel-use scenarios are especially likely to arise if you manage your NixOS fleet with coding agents.  In an agent workflow, the example might look something like this: start one coding agent per worktree in separate windows, prompt each of them with their respective task, then walk away to make some tea.  Given access to nixconvoy and instructions to use it, a decent agent will make sure to deploy their changes to your fleet and verify it actually works before declaring their work finished.

After an agent reports that it's done, review the diff of all of the code changes it has deployed for this task using `nixconvoy review`.  If it still needs more work, prompt the agent some more.  If it's irredeemable garbage, wipe your machines clean of it with `nixconvoy undeploy --back-to origin/main`.  Finally, when you're happy with its work, `nixconvoy land` to promote the change to your main git branch and rebase all local worktrees on top of it (which will make every other agent notice it and consider it authoritative).

> [!TIP]
> `nixconvoy land` can also be used early and proactively to multitask on not-fully-independent config changes, at the price of a bit of git-main-branch tidiness.  For example, if you notice one agent's deploys are starting to fail because it ended up touching the same line of config as another agent, you can `nixconvoy land` one side early to end the deadlock.  Later on, `nixconvoy land` that same agent a second time when its work is fully complete.


# Basic concepts

## Command-line reference

```xml
Setting up a repository:
  nixconvoy init [<flake-dir>]

Managing worktrees:
  nixconvoy open <worktree-path> [--branch <workbranch>]
  nixconvoy open --child <worktree-path>
  nixconvoy open --submodule <submodule>
  nixconvoy close [<worktree-path> ... | <submodule> ...] [--force]
  nixconvoy pull
  nixconvoy status
  nixconvoy hook <event> < <hook-input>

Deploying and committing changes:
  nixconvoy deploy [<target> | @<tag> ...] [-m <msg>] [--evict] [--dry-activate]
  nixconvoy undeploy [<worktree> ... | --with-children] [--back-to <rev>]
  nixconvoy review [<worktree> | <submodule> | <workbranch> ... | --with-children] [<diff-arg> ...]
  nixconvoy land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]
```

## Checkouts

Nixconvoy distinguishes two kinds of checkout.  Your **main checkout** is the
normal git checkout you had on your workstation before you adopted nixconvoy.  You can keep landing
changes to its `main` branch with regular git commands and then deploy them with nixconvoy.  **Nixconvoy
worktrees** are for work-in-progress changes.  These are created using `nixconvoy open <worktree-path> [--branch <workbranch-name>]`, removed with `nixconvoy close`, and carry their own workbranch.

Some rules follow from this:

- Most nixconvoy subcommands read from `main` or `origin/main`, but the only one that writes to `main` is `nixconvoy land`.
- In a worktree, write only via `nixconvoy` commands, never raw `git`, and generally deploy before you land.

> [!NOTE]
> Strictly speaking, worktrees and workbranches are separate concepts, corresponding to git worktrees and git branches respectively.  Some nixconvoy subcommands operate on git worktrees, some on git branches, and some both at the same time.  But in the nixconvoy mental model, these are mostly 1:1.   `nixconvoy open` creates both a worktree and its dedicated branch, `nixconvoy close` removes both, and the other subcommands generally assume each worktree hasn't been switched away from the branch that nixconvoy created for it.

There are three ways to use worktrees, depending on your preference:
- **Long-lived worktrees** with a generic name.  This is how I prefer to use nixconvoy most of the time.  For my homelab, my main checkout lives in `~/projects/homelab` while I have 4 nixconvoy worktrees `~/projects/homelab1`, ...,  `~/projects/homelab4`.  I created them with a command like:

  ```console
  $ cd ~/projects/homelab
  $ nixconvoy open ../homelab3 --branch 3
  ```

  I never `nixconvoy close` them.  `nixconvoy land` brings all 5 checkouts up to date automatically, and then I reuse a numbered worktree at random for the next change.
- **Short-lived worktrees** with a task-specific name.  For example:

  ```console
  $ cd ~/projects/homelab
  $ nixconvoy open ../homelab-jellyfin
  ```

  Once the feature is deployed and landed, close them.  This is about as fast and easy to work with as long-lived worktrees --- the choice between them comes down to taste.
- **Child worktrees.**  Child worktrees are opened from another nixconvoy worktree either using `nixconvoy open --child` or the Claude Code subagent plugin.  Their files live in a subdirectory of their parent worktree.  This is a more advanced feature that can be useful to manage independent portions of one larger task.  All the child worktrees for a task can be undeployed, reviewed or landed as a group from the parent worktree using the `--with-children` parameter.  

## Deploying and undeploying

```xml
  nixconvoy deploy [<target> | @<tag> ...] [-m <msg>] [--evict] [--dry-activate]
  nixconvoy undeploy [<worktree> ... | --with-children] [--back-to <rev>]
```

These are the fundamental actions to apply changes to your running fleet.  

You run `deploy` after staging a change in a workbranch.  `nixconvoy deploy` both commits it to the workbranch and applies it to your machines.

**Guarantees:**
- **Nothing you deploy is ever silently undone.**  What gets applied is not just what is in your current branch, but the union of everything previously deployed from every ongoing workbranch.
  - Deploy aborts on a true merge conflict between two workbranches. Because nixconvoy merges are syntax-aware, adjacent-line edits aren't enough to cause an abort.
- **No change will touch a target machine before passing prechecks (lint/static analysis).**
  - To reduce the performance cost of this guarantee, precheck runs in parallel with eval.
- **A deploy running at the same time as another `deploy` (or `land`) will serialize correctly.** In most cases, deploys will queue after one another; in some cases, nixconvoy will abort one side and ask you to retry.  Either way, your git repo and NixOS fleet will remain in a consistent state throughout.
- **A broken NixOS generation automatically rolls itself back.** If a target loses connectivity or fails its health check after switching, it rolls back automatically.
- **Nix configuration changes are simultaneously deployed to all target machines that they affect.**
  - Unless you provide an explicit `<target>`/`@<tag>` parameter, which restricts the set of targets considered.

> [!NOTE]
> To reduce the performance cost of this final guarantee, nixconvoy includes a feature called the "eval memo".  This reuses previous eval results to decide which targets could possibly be affected by a deploy.  The memo logic may have false positives (unnecessary re-evaluations) but will never have false negatives.
> 
> The memo is keyed by a list of filenames and their content hashes.  So if you have a heterogeneous fleet, I recommend organizing your Nix config into per-feature files to fully take advantage of it.

**Optional flags:**

| `deploy` flag | |
|---|---|
| `<target>` | Restrict the deploy to only this machine.  If none is specified, all affected machines will be deployed to.  Can list more than one, separated by spaces.<br><br>(In general, I recommend not going out of your way to specify targets unless it's important for safety reasons not to deploy to certain machines.) |
| `@<tag>` | Similar to `<target>` --- restrict the deploy to only machines with this tag. |
| `-m <msg>` | The commit message on the workbranch for this deploy increment.  (Note that these commit messages get hidden in nixconvoy's internal branches after `nixconvoy land` so it's optional.) |
| `--evict` | Deploy exactly what's on this branch, skipping over nixconvoy's usual union-of-workbranches step and forcing the target to match it even if that would violate nixconvoy's normal guarantees.<br><br>Called "evict" because it evicts all other workbranches's ongoing work from the target.  This is a recovery command in case something went wrong and nixconvoy's union mechanism or safety systems are wedged.  |
| `--dry-activate` | Run prechecks, evaluation, and build, but don't actually switch the machines to the new NixOS generation. |

`nixconvoy undeploy` is the reverse of deploy.  Run without flags, it undoes the latest deploy.

| `undeploy` flag | |
|---|---|
| `<worktree>` | the name of a specific child worktree to undeploy. |
| `--with-children` | undeploy all child worktrees of this parent worktree.  Same effect as listing all child worktrees one by one. |
| `--back-to <rev>` | the revision to undeploy back to.  `--back-to HEAD^1` does the same as no flags.  `--back-to origin/main` undeploys all unlanded changes in this workbranch, effectively  turning it back into a freshly opened workbranch. |

## Reviewing and landing

```xml
  nixconvoy review [<worktree> | <submodule> | <workbranch> ... | --with-children] [<diff-arg> ...]
  nixconvoy land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]
```

`nixconvoy review` shows the diff all changes that were deployed on this workbranch since it diverged from `main`.  With a simple workbranch, this is mostly a convenience sugar roughly equivalent to `git diff origin/main`.  If you are using submodules or child worktrees, it's more valuable, as it lets you review the changes on all of them in one diff.

`nixconvoy land` squashes all changes on the workbranch into one commit, lands it on `main`, and pushes it to `origin/main`.  It also rebases all other worktrees to include the commit, if possible to do without conflict.

> [!NOTE]
> If there is a conflict, the other worktree will be untouched and flagged in `land`'s stdout, and the next deploy on that worktree will try to rebase onto `main` again.

| `land` flag | |
|---|---|
| `<worktree>` | the name of a specific child worktree to land, if you're using child worktrees. |
| `--with-children` | land all child worktrees of this parent worktree.  Same effect as listing all child worktrees one by one. |
| `-m <msg>` | A one-liner commit message.  Will prevent the EDITOR from showing up.  Note that the EDITOR also won't come up if a `COMMIT_MSG.md` is present and the terminal is not interactive (this behavior is intended to allow agents to write up their own commit message while you approve by typing `!land`.) |
| `--not-yet-deployed` | Override a default safety feature of nixconvoy designed to avoid unpleasant surprises --- namely, that `nixconvoy land` will refuse if not all affected targets are deployed to yet.  If you want to land anyway (e.g. because that machine is powered off at the moment and it is safe to implicitly deploy that change later), then specify this flag. |

# Configuration

## Example nixconvoy.toml

`nixconvoy init` will generate a basic version of this config file at the root of your repo after briefly autodetecting your flake config.  To illustrate the ways you can configure nixconvoy using it, here is a more complex `nixconvoy.toml` configuration example inspired by the one I use for my homelab fleet:

```toml
config_version = 1
flake_dir = "nixos"
targets_dir = "nixos/hosts"
targets_arch = "x86_64-linux"
ssh_address_template = "bluquark@{target}.home.bluquark.net"
branch_prefix = "work/{machine}/"
child_worktrees = ".claude/worktrees"
precheck = "repo-lint"

# On my macbook, the arch is not "x86_64-linux" so use the minipc as NixOS store and builder
[hostnames.macbook]
store = "minipc"

# My Windows's machine's hostname doesn't match the WSL NixOS target name
[hostname.windows11]
target = "windows-wsl"

# All NixOS generations must accept fresh ssh connections and retain Internet access
[switch]
auto_rollback_checks = [{ ssh_deadline_secs = 10 }, { internet = true }]

[targets.minipc]
tags = ["lan"]

# My NixOS-based router must continue to provide Internet and DNS access to others
[targets.router]
[targets.router.switch]
auto_rollback_checks = [
  { routing = true },
  { bash = '''
systemctl is-active --quiet dnsmasq || { echo "dnsmasq is not active" >&2; exit 1; }
pid=$(systemctl show -p MainPID --value dnsmasq)
[ "$pid" != 0 ] || { echo "dnsmasq is not running" >&2; exit 1; }
''' },
]

[targets.windows-wsl]
tags = ["lan"]
# No need for auto-rollback on WSL
switch = { auto_rollback_checks = false }
# This target's hostname doesn't correspond to its domain name
ssh_address = "bluquark@windows.home.bluquark.net"

[targets.hetzner1]
tags = ["vps"]
[targets.hetzner2]
tags = ["vps"]
```
## AGENTS.md

Add some text such as the following into the `AGENTS.md` file in your repo root:

```markdown
In this repo, `nixconvoy` (github.com/bluquark/nixconvoy) is the only sanctioned way to invoke
nixos-rebuild switch and git commit.  It is possible there are other agents that are deploying their
changes in parallel in other worktrees on this machine (you can survey what they're doing with
`nixconvoy status`).  Nixconvoy will make sure all not-yet landed changes deployed from each
worktree are automatically merged together into a union commit by mergiraf before getting applied to
target machines, and also that changes landed to origin/main are automatically rebased on your
checkout (can also be done manually using `nixconvoy pull`).

Repo rules:
- Go ahead and deploy (`nixconvoy deploy -m "what this increment changed"`) as soon as a change that
  affects a NixOS target is staged.
- Never run git commit yourself: stage your changes and let `nixconvoy deploy` commit them onto this
  checkout's work branch.
- Maintain a staged file called COMMIT_MSG.md at the repo root: a concise summary of this checkout's
  work since it started to diverge from `origin/main`.  It will be consumed by `nixconvoy land`.
  The format is a max 100 character subject line: and if that work spans multiple unrelated tasks, a
  max 100 character bullet point per subtask.
- Do not run `nixconvoy land` yourself without explicit authorization.  Instead, when your change is
  finalized, ask the user to review the sum of all deployed increments using `nixconvoy review` and
  decide whether to land them onto the main branch using `nixconvoy land`.
- Increasing NixOS deploy time by 1s or more is not acceptable without a good reason.  If your
  change carries a risk of this, examine `.cache/nixconvoy/logs/perf.jsonl` to verify impact on
  eval, build and switch.
```

> [!NOTE]
> This text is just an initial suggestion based on what I've found nudges agents in the right direction, it is not magic sauce to make them behave perfectly.  Adjust to taste if you have different preferences, and add clarifications whenever you notice one of them got confused about something.

# How it works

This section explains some of the key design decisions and implementation details of nixconvoy.  You don't need to understand all of this simply to use nixconvoy --- but it may be useful especially when troubleshooting, optimizing deploy performance, or understanding how to set up a NixOS fleet or a Git setup that works well with it.  Also, if some of the behaviors of nixconvoy strike you as overly "magical", this should help demystify them.

<details>
<summary><b>Detailed stages of <code>nixconvoy deploy</code></b></summary>


1. **Survey the environment**.  Fetch origin/main from GitHub (or whatever you're using as your remote) and ask every target machine what it's running.  If any target is unreachable, the deploy is aborted (if so, you can retry, with a more restricted list of targets).  If another `nixconvoy deploy` is actively running against the same target machines, wait until it exits before moving onto the next step.
2. **Compute the change to apply**. In a hidden worktree internally managed by nixconvoy, take your latest staged changes, rebase them onto `origin/main`, and merge in all deployed-but-not-yet-landed changes from other work branches (if any) using mergiraf.  If mergiraf fails (usually because another deploy changed the exact same line), the deploy is aborted.  (If so, you can land the competing branch and then rebase on top of it.)
3. In parallel:
	1. Run an optional **precheck**, usually a flake check running a bunch of lint or static analysis tools.  If one of these returns an error, the deploy is aborted.
	2. **Evaluate** targets.  Kick off Nix evals, one for each target machine.  Optimizations:
		1. Evals run concurrently with each other.
		2. Only targets that could possibly be affected by file you changed are re-evaluated -- others reuse the memoized result from the last evaluation.  (To take advantage of this optimization, make sure to keep per-host modules in files other hosts don't import.)
4. **Build** the new closures (including any unit tests you've included as part of your build), on your remote builder if you have one.  This only happens for the targets whose running system's eval result actually turn out to differ.  If anything fails to build (or test, if you include them as part of your build phase), the deploy is aborted.
5. **Record** into git, phase 1.  Each target’s record commit is attached to the deploy’s announcement on the remote.
6. **Switch**. Copy the closures to the targets and switch their NixOS generation to the new one.  Safety features:
	1. If another switch somehow switched some targets but then somehow died before the following "record" step, then abort.  This should be very rare (for example, it may happen due to a power outage or a bug in nixconvoy) --- if it does, you can recover the affected targets using the `--evict` flag.
	2. If, within a deadline of the activation's end (10 seconds by default), the new system isn't found running over a fresh ssh connection with every configured check passing, the target machine automatically rolls itself back.  This is done using an executable running on the target, so it works even if the network was taken down.
7. **Record** into git, phase 2.  Only after every switch succeeded, commit the incremental change onto your work branch and push it to your git remote.

Also, there are two special cases: a change touching only documentation skips everything except the precheck and is simply committed immediately.  And if `--dry-activate` was specified, the process stops after step 4 and simply reports what would have gotten switched.

</details>

### Git-managed state

<details>
<summary><b>Branches</b></summary>

- **`main`.**  Contains landed commits only.  Written by `nixconvoy land` (or by hand using a normal `git commit`, usually from the primary checkout).
- **Work branches `nixconvoy/<branch>`.** One per checkout, conceptually "owned" by the checkout that has it checked out.  One commit per successful deploy.  Reset onto main by `land` and `undeploy`.  Nixconvoy expects that you'll never commit or force-push to one of these using raw `git` --- always use `nixconvoy`'s subcommands to interact with one of these branches, or nixconvoy may get confused later.
- **Child branches `<parent branch>-<name>`** (`nixconvoy/<host>/main-<name>` under a main checkout).  This optional feature of nixconvoy comes into play if you use `nixconvoy open --child` or if you have the Claude Code nixconvoy plugin installed and kick off work inside a subagent.  Created on the child worktree’s first deploy, can be interacted `--with-children`.
- **`nixconvoy-history/<target>`.** Each machine’s full deploy history, updated on every deploy which affected that target.  Each record is a commit of the exact configuration the target runs, whose parents are the previous record, origin/main, and each mergiraf'ed work branch’s tip.  It also described what was merged to produce it and who deployed it.  With this, you can examine the history of everything nixconvoy has ever applied to your machines: for example, using `git log --first-parent origin/nixconvoy-history/<target>` and `git diff origin/nixconvoy-history/<a> origin/nixconvoy-history/<b>`.

</details>

<details>
<summary><b>Refs</b></summary>

- **`refs/nixconvoy/deploying/branch/<branch>`.** An "announcement" mechanism registering the fact that a nixconvoy deploy is in progress, renewed every 10s.  This is used to inform other invocations of nixconvoy that they should wait instead of running in parallel.
	- Normally, this is removed after a deploy completes, but I can't rule out the possibility (e.g. due to a power outage or a bug in nixconvoy) that one of these may accidentally persist and block all deploys indefinitely.  In that case, the failure message will suggest to you a `git push` command to clear it by hand.

</details>

<details>
<summary><b>Commit message convention</b></summary>

- Default message for work branch commits generated by `nixconvoy deploy`: `<checkout>: increment <n>`. Trailers: `Reaches:` (targets whose evaluation reads a changed file) and `Deployed:` (targets actually switched to the latest configuration).
	- A target in `Reaches` but not `Deploye`d block other work branches's `deploy` from touching that machine, to avoid accidentally deploying in work that wasn't intended to affect that machine yet.
	- It will also block `land` from running, which can be overridden by running `nixconvoy land --not-yet-deployed`

> **Tip:** If that sounded confusing, just always run `nixconvoy deploy` without going out of your way to specify which target machine to restrict the change to.  Then, `Reaches` and `Deployed` will always be the same and you won't run into this scenario.  `nixconvoy` assumes you'll only manually specify targets when you have a good reason to restrict the deploy to only those targets (e.g. you only want to deploy a risky change to a test machine, not the prod machine).

</details>

### Filesystem state (ordinary files)

<details>
<summary><b>Files in your git checkout</b></summary>

- **`nixconvoy.toml`.**  Nixconvoy's main user-editable config file.  The usual workflow is to create this with `nixconvoy init` on first install, then manually edit it as needed thereafter.  (This is intended to be committed into git main branch.)
- **`COMMIT_MSG.md.`** This optional file stores the draft of the `nixconvoy land` commit message for the current task.  Since the recommended AGENTS.md clause discourages agents from calling `nixconvoy land` themselves, this provides them a place to prewrite the final commit message that will go onto the main branch.   (This file is intended to only be committed to workbranches.  Nixconvoy never commits this file to the main branch, and also specially excludes it from the mergiraf, so it cannot cause a deploy to fail with a merge conflict.)

</details>

<details>
<summary><b>Local state elsewhere on the machine running nixconvoy</b></summary>

- **`/tmp/nixconvoy-deploy-wt.*`**.  The temporary worktree the union commit is merged inside of.
- **`~/.cache/nixconvoy/eval-memo/`**.  The eval memo keyed on git blob hashes.  This is used to enable the optimization "Only targets that could possibly be affected by file you changed are re-evaluated"
- **`~/.cache/logs/`**.  Logs of nixconvoy deploys and worktree prunes.  May be useful to diagnose bugs and performance problems.
- ``<main-checkout>/.git/worktrees/<name>/nixconvoy/``. A deploy lock & background push state file.
- **`<main-checkout>/.git/nixconvoy/`**.  Worktree open/close locks, a copy of the branch prefix --- as well as the main checkout's deploy lock & background push state file.

</details>

<details>
<summary><b>Local state on the target NixOS machines</b></summary>

- `/var/lib/nixconvoy/provenance.json`: a record of the mergiraf union, each branch tip, which machine and checkout deployed it, and when.  (Used for safety checks at the switch stage of `nixconvoy deploy`.  Unless something went wrong, this file entirely duplicative of state tracked in git.)

</details>

## Codebase

Nixconvoy is written in a mix of Python and Rust.  Python is used for the script-like code, while Rust is used for core logic, particularly the `deploy` state machine (with its multiple error conditions and concurrency).  The Rust core is tested using stateright and property tests.

The code was written via coding agent, mostly Fable.  This README you're reading was written artisanally in a text editor.  Either way, I take responsibility for the quality of the result.

# Advanced features

<details>
<summary><b>Submodules</b></summary>
<br>
Nixconvoy has support for git submodules.  If you don't write your own software or substantially modify the software packages you use on your NixOS fleet, you can safely ignore this feature.

Submodules are one of git's mechanisms for loading a dependency (whose source of truth is an external git repo) into one of your checkout's subdirectories.  This is particularly useful for developers who maintain a public repository and also run it as part of their private NixOS fleet (in my own environment, I use nixconvoy's submodule support to work on nixconvoy itself).  It can also be used for your private fork of a dependency --- although for that, git subtrees are a simpler alternative that also works.

In your main NixOS config repo, create a gitlink for it using `git submodule add --name <input> <url> <path>`.  Then, target the flake in your nix config to use that submodule directory instead of the usual public repository, using a relative path input (`path:./<path>`) with `inputs.self.submodules = true`.  After a submodule is registered with nixconvoy, do not use raw `git` commands to modify it.

Then, when you're ready to make a private change to it, open a nixconvoy worktree using `nixconvoy open`, then populate the subdirectory using `nixconvoy open --submodule`.  All the other features of nixconvoy compose nicely with submodules.  You can stage a change inside one and run `nixconvoy deploy` from the worktree root as usual.

</details>

<details>
<summary><b>Bootable generation assessment</b></summary>
<br>
Nixconvoy includes an optional module to have smarter behavior for which NixOS generations are selectable on the boot screen.  Compatible with lanzaboote only, and not enabled by default.

Using nixconvoy as intended can rapidly create a rather large number of NixOS generations, so if your `/boot` partition is on the smaller side, lanzaboote's default "preserve latest N generations" will preserve a bunch of near-identical generations --- not a terribly useful backup plan if the change that broke boot was deployed, say, N+5 generations ago.  So on each nixconvoy deploy, the module performs its own custom generation pruning by age and by proven bootability.

</details>

# Limitations and alternatives

## Missing features

There are some features that other NixOS deployment tools support but nixconvoy doesn't, either because I didn't get around to them yet or because they're fundamentally hard to square with nixconvoy's design goals:

* nixconvoy only supports flake-based NixOS configurations.
* The build host and targets must all be Linux, all of the same CPU architecture.
* nixconvoy does not support boot or test goals (i.e. all nixconvoy deploys switch the target to the new generation without delay).
* Nixconvoy's ssh user must have passwordless sudo on the entire NixOS fleet.
* No offline deploys --- every deploy requires the Git origin to be reachable.

If one of these is a hard blocker for you, consider an alternative NixOS deployment tool, such as deploy-rs or colmena.

## Deploy-then-land bias

nixconvoy has a "soft" limitation in its overall design: generally speaking, it assumes that you'll deploy changes onto your fleet first, and land them onto your main branch once they're proven to work.  In other words, a "test in production & rollback if it breaks" kind of workflow.  That is how I prefer to work, at least when I'm not maintaining mission-critical infrastructure (and currently I'm not).  It's just faster, and one of nixconvoy's primary goals is to enable you to work fast.

The reverse order (land then deploy) is certainly supported --- two ways to do it are:

* committing using plain `git commit` on the `main` branch followed by `nixconvoy deploy`
* running e.g. `nixconvoy deploy @test-vms` first, followed by `nixconvoy land --not-yet-deployed`, followed by `nixconvoy deploy`

As the `@test-vms` example gestures at, one reason you might want to "land before deploying" is if you want to test a change in an experimental environment before it makes it to any production machine.  Another reason is if you have teammates that are required to code-review your change.

If you have such "enterprisey" needs, it may be possible to adapt nixconvoy to your use case, for example by giving developers ssh keys only to test machines, while the real production fleet has a builder with a private checkout with a job that runs `nixconvoy pull` and `nixconvoy deploy` on a regular basis.  (Indeed, this is similar to how I do nightly security updates on my NixOS fleet.)  But you may also find it more natural to use a deploy tool designed primarily for pull-based GitOps, such as comin.
