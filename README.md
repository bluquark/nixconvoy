# Nixconvoy

<!-- Github About: A NixOS deployment tool designed for multitasking -->

Nixconvoy is a NixOS deployment tool with a simple command-line interface.  Its distinguishing features are:

* Nixconvoy maintains a detailed record of every change deployed to your NixOS fleet --- allowing you to reconstruct what happened and revert exactly to any previous state.  It does this largely behind the scenes using its own git branches, so your main git branch stays tidy.
* Nixconvoy is designed for iterative experimentation on live machines.  If your change breaks ssh connectivity (or any other health check of your choice), then nixconvoy automatically rolls back to the previous NixOS generation.
* Nixconvoy is designed for multitasking.  It merges all of your work-in-progress changes together before every deploy, so one half-finished change never undoes another.  And it's designed to serialize correctly when two or more nixconvoy instances are running at the same time.

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
Audit all of my server's systemd units for security hardening opportunities.
Then divide them into 10 nonconflicting groups and launch one subagent to harden each.
```

The 10 subagents will go off and work for a while, and as long as they don't touch literally the same lines of config, they should be able to `nixconvoy deploy` their respective changes to verify they didn't break anything on the real machine before terminating.  Then, from the parent worktree, you can examine their config changes as a whole with `nixconvoy review --with-children`, and then land all of it as one single commit on your main branch with `nixconvoy land --with-children`.

> **Warning:** Claude Code doesn't guarantee a stable API for plugins, so this plugin is the most likely part of nixconvoy to break over time.  It may also conflict with other Claude Code hooks or plugins if you have any installed.  So I only recommend it for people who heavily use subagents to write code (not just to research or codereview).

</details>


# Tutorial

To begin with a concrete example, let's say you are maintaining a nascent NixOS homelab with one workstation and one headless server.  Your git remote is a private repo called "homelab" on GitHub, and you have a local clone of it on your workstation called `~/homelab`.  It already includes a working NixOS configuration for your server that you've previously deployed using `nixos-rebuild`.

## Basic use

After following the Installation instructions above to set `~/homelab` up with nixconvoy, the next step is to create your first nixconvoy-managed worktree, using `nixconvoy open`.

> [!NOTE]
> `nixconvoy open <worktree-path> [--branch <branch-suffix>]` creates a new worktree (by invoking `git worktree add` internally), then switches it to a new work branch prefixed by `nixconvoy/` (or any other pattern of your choice specified by the `branch_prefix` setting).
> 
> As a reminder, a git worktree is a directory containing another checkout of a local git repo, linked to it in a parent/child relationship tracked by git.  Worktrees function similarly to regular checkouts, except they don't have a `.git` directory: their git objects are stored in their parent's.  This means creating/deleting worktrees can be as safe and lightweight as switching branches within a single checkout.

Then, stage a change and run `nixconvoy deploy` --- this subcommand commits your changes to the branch nixconvoy created for the worktree, and applies a new NixOS generation to your server.  You can repeatedly stage more incremental changes and `deploy` them.  When you're done, `nixconvoy land` rolls up the entire series of deployed changes into one git commit, and applies it to both `main` and `origin/main`.

Concretely:

```console
$ cd ~/homelab/
$ nixconvoy open ../homelab-firewall

$ cd ~/homelab-firewall/
# vim nixos/hosts/server/firewall.nix
$ git add -A
$ nixconvoy deploy
$
$ vim nixos/hosts/server/nftables.nix
$ git add -A
$ nixconvoy deploy
$
$ nixconvoy land -m "Tighten firewall"

$ cd ~/homelab/
$ nixconvoy close ../homelab-firewall
```

> [!NOTE]
> As illustrated above, the default way to use nixconvoy is to deploy-then-land.  What if you prefer to land-then-deploy?
>
> There are two ways:
> 1. On your `main` branch, commit using plain `git commit`, then  `nixconvoy deploy`
> 2. On a work branch, `nixconvoy deploy @test-vms` (e.g.), then `nixconvoy land --not-yet-deployed`, then `nixconvoy deploy`

## Basic multitasking

Now, let's assume you have two independent improvements you want to work on today for your server: 1) tighten your firewall security to protect your machine against a recent CVE, and 2) install Jellyfin.

You can spin up one worktree per task:

```console
$ cd ~/homelab/
$ nixconvoy open ~/homelab-firewall
$ nixconvoy open ~/homelab-jellyfin
```

Now you can safely work on both features and deploy their incremental changes in any order:

```console
$ cd ~/homelab-firewall/
$ vim nixos/hosts/server/firewall.nix
$ git add -A
$ nixconvoy deploy

$ # Hmm, not quite done, I need to research this more, let's work on jellyfin for now
$ cd ~/homelab-jellyfin/
$ vim -o nixos/hosts/server/configuration.nix nixos/hosts/server/jellyfin.nix
$ git add -A
$ nixconvoy deploy
$ cd ~

$ # Back to the half-finished firewall task...
$ cd ~/homelab-firewall/
$ vim nixos/hosts/server/nftables.nix
$ git add -A
$ nixconvoy deploy

$ # All done for the day, land both features and clean up
$ cd ~/homelab-firewall/
$ nixconvoy land -m "Tighten firewall"
$ cd ~/homelab-jellyfin/
$ nixconvoy land -m "Add Jellyfin"
$
$ cd ~
$ nixconvoy close ~/homelab-firewall
$ nixconvoy close ~/homelab-jellyfin
```

Notice the ordering of the config changes and deploys.  `~/homelab-jellyfin/` doesn't have the changes in `firewall.nix`, and vice versa.  So if you tried this with ordinary git checkouts and ordinary `nixos-rebuild`, the first round of security-critical firewall changes would accidentally get removed from the server when Jellyfin was deployed --- then later, Jellyfin would get uninstalled when the second round of firewall changes was deployed!

But this multitasking difficulty doesn't arise with nixconvoy --- assuming each edit being made to the .nix files is purely adding lines, then nixconvoy guarantees that each `deploy` will be purely additive as well.

Absence-means-removal is one of the best properties of NixOS, but for me, it sometimes got in the way of casual multitasking --- when I just want to deploy stuff from different checkouts without thinking too hard about sequencing and version control.  The initial desire motivating nixconvoy was, what if there existed a deploy tool where accidental removal "magically" doesn't happen?

> [!NOTE]
> So what's the trick?  Nixconvoy uses git worktrees and refs to keep track of everything you have been deploying.  Instead of deploying what's in your current checkout, it merges your latest staged changes with other checkouts' deployed-but-not-landed changes, then builds the NixOS generation from that.
>
> Merge conflicts are also less frequent with nixconvoy than with a default installation of `git merge`, because nixconvoy bundles in a syntax-aware merge tool called `mergiraf`.

## Multitasking with coding agents

Multitasking scenarios are especially likely to arise if you manage your NixOS fleet with coding agents.  In an agent workflow, the example might look something like this: start one coding agent per worktree in separate windows, prompt each of them with their respective task, then walk away to make some tea.  Given access to nixconvoy and instructions to use it (see suggested `AGENTS.md` text below), a decent agent will make sure to deploy their changes to your fleet and verify it actually works before declaring their work finished.

After an agent reports that it's done, review the diff of all of the code changes it has deployed for this task using `nixconvoy review`.  If it still needs more work, prompt the agent some more.  If it's irredeemable garbage, wipe your machines clean of it with `nixconvoy undeploy --back-to origin/main`.  Finally, when you're happy with its work, `nixconvoy land` to promote the change to your main git branch and rebase all local worktrees on top of it (which will make every other agent notice it and consider it authoritative).

> [!TIP]
> `nixconvoy land` can also be used early and proactively to multitask on not-fully-independent config changes, at the price of a bit of git-main-branch tidiness.  For example, if you notice one agent's deploys are starting to fail because it ended up touching the same line of config as another agent, you can `nixconvoy land` one side early to end the deadlock.  Later on, `nixconvoy land` that same agent a second time when its work is fully complete.


# User guide

## Command-line overview

```xml
Setting up a repository:
  nixconvoy init [<flake-dir>]

Managing worktrees:
  nixconvoy open <worktree-path> [--branch <branch-suffix>]
  nixconvoy open --child <worktree-path>
  nixconvoy open --submodule <submodule>
  nixconvoy close [<worktree-path> ... | <submodule> ...] [--force]
  nixconvoy pull
  nixconvoy status
  nixconvoy hook <event> < <hook-input>

Deploying and committing changes:
  nixconvoy deploy [<target> | @<tag> ...] [-m <msg>] [--evict] [--dry-activate]
  nixconvoy undeploy [<worktree> ... | --with-children] [--back-to <rev>] [--dry-activate]
  nixconvoy review [<worktree> | <submodule> | <work-branch> ... | --with-children] [<diff-arg> ...]
  nixconvoy land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]
```

## Checkouts

Nixconvoy distinguishes two kinds of checkout.

Your **main checkout** is the
normal git checkout you had on your workstation before you adopted nixconvoy.  Nixconvoy stores data in it, but does not claim full ownership over it. You can keep landing changes to its `main` branch with regular git commands.

> [!NOTE]
> Most nixconvoy subcommands *read* from the `main` branch, but the only one that *writes* to it is `nixconvoy land`.

**Nixconvoy
worktrees** are for work-in-progress changes.  These are created using `nixconvoy open <worktree-path> [--branch <branch-suffix>]` and removed with `nixconvoy close`.  They carry their own work branch, which should only be committed to using `nixconvoy deploy`.

> [!NOTE]
> Strictly speaking, worktrees and work branches are separate concepts, corresponding to git worktrees and git branches respectively.  But in the nixconvoy mental model, these are mostly 1:1.   Nixconvoy subcommands generally assume a worktree hasn't been manually switched away from the work branch that nixconvoy created for it.

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
  nixconvoy undeploy [<worktree> ... | --with-children] [--back-to <rev>] [--dry-activate]
```

`deploy` and `undeploy` are nixconvoy's core subcommands, which apply new NixOS generations to your fleet.

At a high level, here are the "transactional" properties they are designed to provide.  Some of these come along with tradeoffs: nixconvoy's philosophy is to provide options and optimizations to keep them manageable.

| Guarantee | Cost | Mitigation |
|---|---|---|
| **Nothing you deploy is ever silently undone.**  The input to the NixOS generation applied to your fleet is not only your current checkout, but the union of everything the open work branches have deployed to that machine. |  Harder to keep track of what config ended up running on a given machine.<br><br>`deploy` can abort on a merge conflict between two work branches. | The union result is visible on `nixconvoy-history/<target>`.<br><br>nixconvoy uses a syntax-aware merge tool called `mergiraf`, so even two edits of the same line don't necessarily cause a merge conflict. |
| **Two deploys running at the same time in different worktrees will serialize correctly.** | Trivial (lockfiles). | |
| **Every deploy runs prechecks (lint/static analysis).** | Additional work on every deploy. | nixconvoy runs precheck in parallel with Nix's eval phase, when there is often CPU to spare. |
| **Every deploy runs auto-rollback checks (connectivity checks).** | Trivial (small script runs after activation). | |
| **No arguments means "deploy to all targets".** | The deploy fails if any machine is turned off or otherwise inaccessible.<br><br>Extra Nix eval runs for targets that turn out to be unchanged. | You can choose to provide an explicit `<target>`/`@<tag>` parameter, which restricts the set of targets considered.<br><br>A nixconvoy optimization called the "eval memo" (additive to Nix's built-in eval cache), and support for Determinate Nix's "parallel eval". |
|  |  |  |

### `nixconvoy deploy`

After you've staged a git change, `nixconvoy deploy` both commits it to the work branch and applies it to your machines.

| `deploy` flag | *(all flags are optional)* |
|---|---|
| `<target>` | Restrict the deploy to only this machine.  If none is specified, all affected machines will be deployed to.  Can list more than one, separated by spaces.<br><br>(In general, I recommend not going out of your way to specify targets unless it's important for safety reasons not to deploy to certain machines.) |
| `@<tag>` | Similar to `<target>` --- restrict the deploy to only machines with this tag. |
| `-m <msg>` | The commit message on the work branch for this deploy increment.  (Note that these commit messages get hidden in nixconvoy's internal branches after `nixconvoy land` so it's optional.) |
| `--evict` | Deploy exactly what's on this branch, skipping over nixconvoy's usual union-of-work-branches step and forcing the target to match it even if that would violate nixconvoy's normal guarantees.<br><br>Called "evict" because it evicts all other work branches' ongoing work from the target.  This is a recovery command in case something went wrong and nixconvoy's union mechanism or safety systems are wedged.  |
| `--dry-activate` | Run prechecks, evaluation, and build, but don't actually switch the machines to the new NixOS generation. |

### `nixconvoy undeploy`

`nixconvoy undeploy` is the reverse of deploy.  Specifically, it resets the work branch to a previous commit, and deploys to all targets affected by that.

`undeploy`'s transactional properties and much of its implementation are identical to `deploy`, but its command-line parameters resemble `review`/`land` (because it also acts on previously deployed commits).

| `undeploy` flag | *(all flags are optional)*  |
|---|---|
| `<worktree>` | the name of a specific child worktree to undeploy. |
| `--with-children` | undeploy all child worktrees of this parent worktree.  Same effect as listing all child worktrees one by one. |
| `--back-to <rev>` | the revision to undeploy back to.  `--back-to HEAD^1` has the same effect as no flags.  `--back-to origin/main` undeploys all unlanded changes in this work branch, effectively turning it back into a freshly opened work branch. |
| `--dry-activate` | Run prechecks, evaluation, and build, but don't actually switch the machines to the new NixOS generation. |

> [!NOTE]
> `undeploy` does not switch back to a previous NixOS generation, it pushes a fresh generation (because it does a "merged union of all work branches" just as `deploy` does).
>
> `undeploy` cannot undo a commit that has already landed onto main.  Use plain `git revert` on the main checkout followed by a regular `deploy`.

## Reviewing and landing

```xml
  nixconvoy review [<worktree> | <submodule> | <work-branch> ... | --with-children] [<diff-arg> ...]
  nixconvoy land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]
```

`nixconvoy review` shows the diff all changes that were deployed on this work branch since it diverged from `main`.  With a simple work branch, this is mostly a convenience sugar roughly equivalent to `git diff origin/main`.  If you are using submodules or child worktrees, it's more valuable, as it lets you review the changes on all of them in one diff.

`nixconvoy land` squashes all changes on the work branch into one commit, lands it on `main`, and pushes it to `origin/main`.  It also rebases all other worktrees to include the commit, if possible to do without conflict.

> [!NOTE]
> If there is a conflict, the other worktree will be untouched and flagged in `land`'s stdout, and the next deploy on that worktree will try to rebase onto `main` again.

| `land` flag | *(all flags are optional)*  |
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

# Prefix for all nixconvoy work branches (visible in `git branch`).
# {local_hostname} can be omitted for concision, if you only ever launch nixconvoy from one machine.
branch_prefix = "nixconvoy/{local_hostname}/"

# The subdirectory child worktrees are placed in.
# I put it inside .claude to be compatible with nixconvoy's Claude Code plugin. 
child_worktrees = ".claude/worktrees"

# List of build machines with fallbacks if the earlier ones are unavailable.
# The {local_hostname} is considered unavailable if it's the wrong CPU architecture (i.e. my Macbook).
builders = ["{local_hostname}", "minipc"]

# Precheck step that runs in parallel with eval.  Should be an executable from your PATH.
precheck = "repo-lint"

# Configure automatic rollback if after activating the new NixOS generation, if:
# A) a fresh ssh connection from the host doesn't reach the target within 10s, or
# B) the target cannot reach the internet
[switch]
auto_rollback_checks = [{ ssh_deadline_secs = 10 }, { internet = true }]

# Targets are named by their flake.
[targets.minipc]
# This tag can be used in `nixconvoy deploy` using the @ notation
tags = ["lan"]

[targets.router]
[targets.router.switch]
# This is a NixOS-based router/gateway providing NAT and DNS to my LAN.
# Configure additional auto-rollback checks for it:
# C) Routing still works (verified by the nixconvoy control machine trying to reach the internet)
# D) DNS still works (verified by script running on target)
auto_rollback_checks = [
  { controller_shell = ['[ "$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" https://www.google.com/generate_204)" = 204 ]'] },
  { target_shell = [
    'systemctl is-active --quiet dnsmasq || { echo "dnsmasq is not active" >&2; exit 1; }',
    '''
pid=$(systemctl show -p MainPID --value dnsmasq)
[ "$pid" != 0 ] || { echo "dnsmasq is not running" >&2; exit 1; }
sockets=$(ss -H -ulnp | grep -F "pid=$pid," || true)
grep -qF " 0.0.0.0:67 " <<<"$sockets" || { echo "dnsmasq has no DHCP socket on port 67" >&2; exit 1; }
'''
  ] },
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
- **`nixconvoy-history/<target>`.** Each machine’s full deploy history, updated on every deploy which affected that target.  Each record is a commit of the exact configuration the target runs, whose parents are the previous record, origin/main, and the commit of each mergiraf'ed work branch.  It also described what was merged to produce it and who deployed it.  With this, you can examine the history of everything nixconvoy has ever applied to your machines: for example, using `git log --first-parent origin/nixconvoy-history/<target>` and `git diff origin/nixconvoy-history/<a> origin/nixconvoy-history/<b>`.

</details>

<details>
<summary><b>Refs</b></summary>

- **`refs/nixconvoy/deploying/branch/<branch>`.** An "announcement" mechanism registering the fact that a nixconvoy deploy is in progress, renewed every 10s.  This is used to inform other invocations of nixconvoy that they should wait instead of running in parallel.
	- Normally, this is removed after a deploy completes, but I can't rule out the possibility (e.g. due to a power outage or a bug in nixconvoy) that one of these may accidentally persist and block all deploys indefinitely.  In that case, the failure message will suggest to you a `git push` command to clear it by hand.
- **`refs/nixconvoy/rebase-map/<branch>`.** Which commit replaced each commit of a work branch when nixconvoy rebased it, so that the commits named in `nixconvoy-history/<target>` can still be found on the branch.

</details>

<details>
<summary><b>Commit message convention</b></summary>

- Default message for work branch commits generated by `nixconvoy deploy`: `<checkout>: increment <n>`. Trailers: `Reaches:` (targets whose evaluation reads a changed file) and `Deployed:` (targets actually switched to the latest configuration).
	- These are informational.  What a work branch has deployed to a target is read from `nixconvoy-history/<target>`: a change it has not yet deployed there is left out when another work branch deploys to that target, to avoid accidentally deploying in work that wasn't intended to affect that machine yet.
	- Such a change will also block `land` from running, which can be overridden by running `nixconvoy land --not-yet-deployed`

> **Tip:** If that sounded confusing, just always run `nixconvoy deploy` without going out of your way to specify which target machine to restrict the change to.  Then, `Reaches` and `Deployed` will always be the same and you won't run into this scenario.  `nixconvoy` assumes you'll only manually specify targets when you have a good reason to restrict the deploy to only those targets (e.g. you only want to deploy a risky change to a test machine, not the prod machine).

</details>

### Filesystem state (ordinary files)

<details>
<summary><b>Files in your git checkout</b></summary>

- **`nixconvoy.toml`.**  Nixconvoy's main user-editable config file.  The usual workflow is to create this with `nixconvoy init` on first install, then manually edit it as needed thereafter.  (This is intended to be committed into git main branch.)
- **`COMMIT_MSG.md.`** This optional file stores the draft of the `nixconvoy land` commit message for the current task.  Since the recommended AGENTS.md clause discourages agents from calling `nixconvoy land` themselves, this provides them a place to prewrite the final commit message that will go onto the main branch.   (This file is intended to only be committed to work branches.  Nixconvoy never commits this file to the main branch, and also specially excludes it from the mergiraf, so it cannot cause a deploy to fail with a merge conflict.)

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

The code was written via coding agent, mostly Fable.  This documentation was written artisanally in a text editor followed by LLM review.

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

Using nixconvoy as intended can rapidly create a rather large number of NixOS generations, so if your `/boot` partition is on the smaller side, lanzaboote's default "preserve latest N generations" might preserve a bunch of near-identical generations --- not a terribly useful backup plan if the change that broke boot was deployed, say, N+5 generations ago.  So on each nixconvoy deploy, the module performs its own custom generation pruning by age and by proven bootability.

</details>

# Limitations

There are some features that other NixOS deployment tools support but nixconvoy doesn't, either because they haven't been a high priority for me or because they're fundamentally hard to square with nixconvoy's design goals:

* nixconvoy only supports flake-based NixOS configurations.
* The build and target machines must all be Linux, all of the same CPU architecture.
* nixconvoy does not support boot or test goals (i.e. all nixconvoy deploys switch the target to the new generation without delay).
* Nixconvoy's ssh user must have passwordless sudo on the entire NixOS fleet.
* No offline deploys --- every deploy requires the Git origin to be reachable.

If one of these is a hard blocker for you, consider an alternative NixOS deployment tool, such as deploy-rs, colmena or comin.
