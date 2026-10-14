# nixconvoy

Deploy a NixOS flake to a few targets from several checkouts at once, without
one checkout ever reverting what another already switched.

nixconvoy is for the way a homelab actually evolves: lots of small,
experimental changes, tested in production, kept honest by NixOS's atomic
switches and generations.  You stage a change, run `nixconvoy apply`, and the
targets that differ from the result are switched.  If the target does not come
back reachable and healthy, the switch rolls itself back.  Only after the
targets run the change is it committed, so a branch never claims more than
what is deployed.

## The model in git terms

- **A work branch is where experiments live.**  Each checkout you deploy from
  owns one, listed by pattern in `deploy-branches` at the repo root.  A
  deploy commits the staged state onto it and pushes it; nothing else
  writes there.
- **The targets run the convoy: origin/main plus every open work branch.**
  Each deploy union-merges the pushed work branches (mergiraf resolves
  structural conflicts) and builds that.  Two checkouts changing unrelated
  modules both stay deployed; a real conflict stops the deploy and names
  the branch.
- **Targets are inferred.**  Each target's ground truth is its running
  system.  Whatever differs from the union is switched; a no-op deploy
  evaluates and switches nothing.  An increment that changes nothing any
  target's evaluation reads (docs, dotfiles) is committed and pushed
  without a deploy: nothing probed, built or switched, so nothing need be
  reachable; the targets' drift waits for the next deploy reaching one,
  or for an apply naming targets.
- **`land` lands a branch on main**, as one commit whose message is the
  branch's `COMMIT_MSG.md`; **`abandon`** discards it.  Either way the
  checkout stays on its work branch, reset onto the new main (a subagent
  worktree too: `close` removes one), and every
  other checkout on the machine is rebased onto it.  Neither touches a
  target: abandoned work stays deployed until the next deploy by anyone,
  which says what it reverts - `abandon --apply-now` runs that deploy at
  once.

A checkout on `main` (or any branch `deploy-branches` does not list) is a
plain checkout: it deploys the convoy too, catching the targets up, but owns
no work.

## What keeps it safe

- **Compare-and-swap switches.**  The switch runs under a lock held on the
  target that first re-checks it still runs what this deploy probed.  A
  deploy that raced another aborts cleanly instead of reverting it, and a
  re-run converges.
- **Announced increments.**  A deploy pushes its increment to a pending ref
  on origin before anything is evaluated, and renews it every 10 seconds
  for as long as it runs.  A deploy starting elsewhere whose change reaches
  a target that one reaches too waits for it (only for deploys announced
  before its own, so two never wait for each other), then builds with
  what it pushed - instead of building without it to be refused once it
  switched.  One whose announcement goes 30 seconds without a renewal is
  taken for dead and no longer waited for.  The push that ends the deploy
  clears the announcement (a main checkout's: the push of main that lands
  its HEAD, by its owner); a deploy ending without a switch withdraws it.
- **Lineage.**  Every target records where its system came from: the union,
  each branch's tip, and the machine and checkout that deployed it.  A
  deploy that would overwrite another checkout's not-yet-pushed increment
  is refused (or leaves that target alone when its own work does not reach
  it) until that deploy is over: its announcement cleared without the push,
  which only the deploy's own end or a hand clear does.  Then the increment
  is abandoned and taken over with a warning.  A deploy that died leaves
  its announcement up, so its targets stay refused - the refusal names the
  dead deploy and the clear that ends it - and nothing is ever taken over
  on a clock.  Work of a branch `deploy-branches` does not list stays
  refused until listed.
- **Held-back targets.**  An increment deployed to named targets alone is
  committed with the targets it reaches (`Reaches:`) and the ones now
  running the branch's tip (`Deployed:`); a target in the first and not the
  second is held back until a deploy from that checkout switches it.  The
  union carries the work, so every other work branch's deploy leaves a held
  target alone, or is refused when its own work reaches it; a main
  checkout's deploy (a nightly's) leaves it behind rather than fail; and
  `land` refuses until `--release` - no restricted apply rolls out
  elsewhere by accident.
- **Deploy records.**  Before a target is switched, its deploy record - a
  commit of the exact tree it will run, on its previous record, main, the
  unioned branches and the increment - is pushed to origin, so no target
  ever runs a state origin does not hold.  Once the target runs it,
  `deployed/<target>` points at it: `git log --first-parent
  origin/deployed/<target>` is the target's deploy history, and `git diff`
  between two such branches what two targets run differently.
- **Confirmed switches.**  With the NixOS module, a switch activates the
  new system with `switch-to-configuration test` under a dead-man timer,
  verifies it from the deploying machine (a new ssh connection - a direct
  read on a local target - finds the new system running, its own checks
  pass, the internet is reachable where asked for), and only then sets the
  profile and bootloader entry; otherwise the target rolls back to what it
  ran.  No reboot is ever
  automatic.
- **Protected units.**  A target can declare named groups of units a
  deploy must never restart unasked (a router's networkd, nftables,
  dnsmasq), each with the reason.  The new system is dry-activated first; a
  switch that would touch one stops the whole deploy, quoting the reason and
  the exact `--allow <name>` command that admits it.
- **Boot assessment** (optional, with lanzaboote).  systemd-boot's boot
  counting as a failover to the last proven generation; a generation the
  target fell back from is never switched to again.
- **Commit after switch.**  An increment that fails to evaluate, build or
  switch is never committed anywhere; its changes stay staged for the
  re-run.

## Installation

nixconvoy is a flake.  Add it as an input, import the NixOS module on each
target, and re-export the command from your own flake so nixconvoy and the
target-side helper are pinned together by your `flake.lock`:

```nix
{
  inputs.nixconvoy = {
    url = "github:bluquark/nixconvoy";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixconvoy, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        nixconvoy.nixosModules.default
        ./hosts/myhost/configuration.nix
      ];
    };
    packages.x86_64-linux.nixconvoy = nixconvoy.packages.x86_64-linux.nixconvoy;
  };
}
```

Then run it from the repo root:

```sh
nix run .#nixconvoy -- apply -m "describe this increment"
```

On a NixOS machine you deploy from, the package in
`environment.systemPackages` puts `nixconvoy` (and `git review`) on PATH.
That is the running system's build, which trails the lock until the
machine itself switches; an unattended deploy that first moves the lock
should run the lock's build (`nix run .#nixconvoy -- apply ...`).

Requirements on each target: `/run/current-system` reachable over ssh with
passwordless sudo for the deploying user (a switch, the lineage file under
`/var/lib/nixconvoy` and a dry activation all need root), and the target's
nixosConfiguration named after its directory under `hosts/`.

## Layout nixconvoy expects

```
deploy-branches     patterns of the work branches (one per line, `*` the wildcard)
nixconvoy.toml      optional settings (below)
COMMIT_MSG.md       on a work branch: the pending landing's commit message
<flake>/flake.nix
<flake>/hosts/<name>/   one directory per nixosConfiguration
```

### nixconvoy.toml

Every key is optional.

```toml
flake = "nixos"              # directory holding flake.nix (default ".")
hosts_dir = "nixos/hosts"    # default "<flake>/hosts"
system = "x86_64-linux"
ssh_address = "me@{target}.lan"   # ssh address template (default "{target}"); the builder's too
branch_prefix = "work/{machine}/"    # {machine}: this machine's hostname: a checkout owns
                                     # "<prefix><N>", a task worktree "<checkout's branch>-<name>"
                                     # ("<prefix>main-<name>" under a plain checkout)
worktrees = "worktrees"      # where `open <name>` puts a task worktree, under the checkout (gitignored)

[precheck]                   # a flake check proven before any build
attr = "repo-lint"           # checks.<system>.<attr>
globs = ["nixos/*.nix", "*.py"]   # the files it reads: its memo keys on exactly these

[memo]                       # an overlay whose attributes map 1:1 onto files
overlay = "nixos/pkgs/default.nix"
namespace = "homelab"        # named as pkgs.homelab.<attr>

[eval]
parallel = true              # Determinate Nix's parallel evaluation

[build]
builder = { darwin = "builder" }     # realise closures there (per platform)
unreachable_hint = "asleep? wake it first"
public_substituters = "https://cache.nixos.org"
[[build.caches]]             # your own caches, asked first
url = "http://builder.lan:5000"
served_by = "builder"        # skipped when building on that builder

[targets.laptop]
local_only = true            # deployable only from itself
ssh_address = "root@10.0.0.9"   # overrides the template
tags = ["portable"]          # `@portable` on the command line names every target tagged so

[annexes.mytool]             # a flake input developed in place (see Annexes)
repo = "~/projects/mytool"   # its repository's checkout here; its origin is what the lock fetches
```

### The NixOS module

```nix
nixconvoy.confirmedSwitch.deadline = 30;        # seconds before an unconfirmed switch rolls back (activation included)
nixconvoy.confirmedSwitch.internetCheck = true; # the check also wants the internet reachable (default off)
nixconvoy.confirmedSwitch.checks = ''           # bash, as root, errexit: the target's own assertions
  systemctl is-active -q nginx.service
'';
nixconvoy.protected.risky-network-reload-or-restart = {  # refused unless `--allow risky-network-reload-or-restart`
  units = [ "systemd-networkd.service" "nftables.service" ];
  why = "risk of breaking essential LAN/WAN functionality";
};
nixconvoy.switchLast = true;                # the router: switched alone, after everything else
```

`nixconvoy.nixosModules.boot-assessment` adds `nixconvoy.bootAssessment.*`
(requires lanzaboote).

## Commands

```
nixconvoy apply [-m <msg>] [--reset] [<target> | @<tag> ...]   deploy
nixconvoy apply --dry-activate [<target> | @<tag> ...]   build and copy, then only report what each switch would do
nixconvoy land [--release] [<worktree> ...]   land the work branch on main (--release: targets its increments held back included)
nixconvoy abandon [--apply-now] [<worktree> ...]   discard it (--apply-now: and deploy, reverting it now)
nixconvoy land --union | abandon [--apply-now] --union   this checkout and its task worktrees as one unit (one main commit)
nixconvoy close [--force] [<worktree> ...]   remove task worktrees holding nothing of their own (--force: whatever they hold)
nixconvoy close [--force] <path> ...   remove an agent checkout likewise, with its work branch
nixconvoy close [--force] --annex <input>   remove an annex worktree likewise
nixconvoy pull                 rebase every checkout on this machine onto origin/main
nixconvoy status               every checkout on this machine, and origin's other branches
nixconvoy review [<worktree> | <branch> ...] [<diff-arg> ...]   the pending landing: COMMIT_MSG.md, then the diff
nixconvoy review --union [<diff-arg> ...]   the checkout's and its task worktrees' together
nixconvoy open [--lock <reason>] <name>   a task worktree <checkout>/<worktrees>/<name> (what the plugin's create hook runs)
nixconvoy open [--branch <work-branch>] <path>   an agent checkout at that path on a work branch of this machine's
nixconvoy open --annex <input>   the flake input developed in place under this checkout (see Annexes)
nixconvoy land --annex <input> | abandon --annex <input>   one annex alone, ahead of the checkout's landing
```

`nixconvoy --help` and the docstring of `src/nixconvoy/cli.py` describe each
step of a deploy in full.

## Worktrees

A git worktree nested in a checkout - `<checkout>/<worktrees>/<name>`,
the directory nixconvoy.toml names (`worktrees/` by default, gitignored),
where `nixconvoy open <name>` makes one and the create hook below runs
it - is that checkout's task worktree: it owns `<checkout's
branch>-<name>` from its first deploy and deploys in parallel with its
parent.  `land` and `abandon` leave it in place like any checkout, idle on
its reset branch; `nixconvoy close <name>` removes it when it holds
nothing of its own (`--force`: whatever it holds), as the sweeps do on
their own once its agent has stopped (a resume of the agent recreates it)
or the session that spawned the agent has exited.  When the task
worktrees each did a piece of their parent's task, `land --union` merges
their branches into the parent's tree and lands it all as one commit
under the parent's COMMIT_MSG.md (`review --union` shows each,
`abandon --union` discards each).  `nixconvoy open <path>` makes a
further agent checkout beside the others instead - a worktree at that
path on a listed work branch of this machine's, numbered after the path's
name - and `close <path>` removes one holding nothing of its own.
`integrations/claude-code/` is a Claude Code plugin holding
the hooks that give every subagent spawned inside a repository such a
worktree (`isolation: worktree` added to the Agent call unless it chose
otherwise), create it under the spawning checkout, close it when its agent
stops and recreate it for its resume, beside the `git review` command and
a bash prompt segment:

```
/plugin install nixconvoy --marketplace bluquark/nixconvoy
```

The package also ships the plugin tree under `share/nixconvoy/claude-code/`
for a repo that wires the hooks itself: a settings.json pointing at
`hooks/worktree-create.sh`, `hooks/worktree-remove.sh`,
`hooks/session-start.sh`, `hooks/subagent-stop.sh`,
`hooks/subagent-isolate.sh` (PreToolUse, matcher `Agent|Task`) and
`hooks/worktree-resume.sh` (PreToolUse, matcher `SendMessage`) there, as
`hooks/hooks.json` does (such a repo should not also enable the plugin, or
both would run).  A repo with a PreToolUse hook of its own on `Agent`
that rewrites the call should run `subagent-isolate.sh` from inside it and
continue from the `updatedInput` it prints, rather than wire both (a
plugin install on top of such a hook does wire both): how Claude Code
combines two hooks' `updatedInput` is not specified.  The hook returns
the rewritten call as allowed, so an `ask` rule on Agent does not prompt
for a spawn it isolated.

## Annexes

A flake input you also develop - a tool of your own, a fork - gets the
same deploy model, without a separate checkout per task.  Name it in
`nixconvoy.toml` (`[annexes.<input>] repo = "..."`, the repository's checkout
on this machine) and gitignore `annexes/`.  Then, from a checkout on a work
branch:

- **`nixconvoy open --annex <input>`** adds a worktree of that repository
  at `<checkout>/annexes/<input>`, on a branch named after the checkout's
  work branch, off the repository's default branch.  Work there as in the
  checkout: stage what is ready to deploy.
- **A deploy carries it as a lock change.**  `apply`
  commits the annex's staged state onto its branch with the `-m` message,
  pushes it to the annex's origin and points the `flake.lock` node at the
  pushed rev (`nix flake lock --override-input` on a scratch copy of the
  flake, so a transitive input the annex moved follows), staged in the
  checkout.  The increment then carries the annex
  like any other edit: the union, the eval memo and the lineage see a lock
  change to an input the targets name.  Two work branches moving the same
  annex meet in the union: the lock is merged node by node, and the node
  both moved gets the merge of their annex branches, made in the annex
  repository (its configured checkout, else a bare mirror under
  `~/.cache/nixconvoy/annexes/`; mergiraf on the conflicts), pushed to the
  annex origin's `refs/nixconvoy/union/<input>/<sha>` and locked there -
  a derived value like the union commit, the same again on a retry; a
  union ref that no branch, announcement or deploy record of origin (nor
  the records' history of the last 90 days) locks any more is deleted by
  the next union, a day after it was pushed (`refs/nixconvoy/stamp/<sha>`
  dates each pushed union and past ref).  Two
  annex branches that conflict are a union conflict like any other.  Once
  one of them lands, every other checkout's catch-up (a deploy, `pull`,
  the landing itself) brings its annex worktree onto the landed default
  branch the way it brings the checkout onto main - fast-forwarded when
  it has nothing of its own, rebased with its staged and unstaged state
  otherwise, pushed and the node relocked on it - and a deploy refuses to
  relock an annex still behind that branch.  An annex rebase that is not
  clean is reported, the annex left as it was, to resolve by hand in the
  annex worktree.
- **`land` lands the annex first:** its tip merged onto the
  repository's default branch (as the union merges it) and squashed there
  as one commit (the
  `COMMIT_MSG.md` in the annex worktree, tracked or not, else the
  checkout's), the node relocked on the default branch as a commit of the
  work branch, then main.  It refuses an annex whose HEAD is not what the
  lock deploys; a deploy refuses one whose worktree was reset below the work the
  lock deploys (nothing of its own left: a hand reset; a rebase or amend
  keeps work of its own and passes, the deploy naming what its relock
  leaves behind), which `abandon --annex` drops deliberately.  `abandon` discards it, resetting it onto the default
  branch.  Either way the annex worktree stays, like the checkout; `land
  --annex <input>` and `abandon --annex <input>` do it for one annex ahead
  of the checkout's own landing, the abandon putting the lock node back to
  origin/main's, staged for the next deploy to revert it on the targets.
  `close --annex <input>` removes the worktree with its branch, here and
  on its origin (the last tip kept under
  `refs/nixconvoy/past/<branch>/<sha>` there, since the deploy records
  lock revs of it, for 90 days after the push), refusing what would
  lose work unless `--force`; a task worktree's annexes go with it.
- **Keep the nightly off annexed inputs.**  An unattended `nix flake
  update` would move the node back to the default branch's tip; let
  landings move it instead.
- **Never work in the repository's own checkout.**  The sweep of the
  checkouts a landing or `pull` runs (every configured repository here,
  an annex worktree of it present or not) brings it
  onto the landed default branch when it sits on that branch, by the
  rebase that brings the main checkout onto main: fast-forwarded when
  it holds nothing of its own, its edits riding along; one whose edits
  the landing touches is left as it was and named.
  `status` lists the
  annexes and flags edits there, and nixconvoy leaves a `nixconvoy-annexed`
  marker in that repository's git dir, so a global pre-commit hook can
  refuse an agent's commit on its default branch outside an annex (`[ -f
  "$(git rev-parse --git-common-dir)/nixconvoy-annexed" ]`; a repository
  setting its own `core.hooksPath`, husky's say, bypasses global hooks).

## How it is built

- `src/nixconvoy/`: the Python driver, which performs git, nix and ssh.
- `switch-guard/`: a Rust crate holding the decisions as Sans-I/O state
  machines, with stateright models and property tests: `nixconvoy-plan` (the
  rebase, the union, the drift verdicts, a subagent worktree's pruning
  against the create hook, the sessions and its agent's stops and
  resumes), `nixconvoy-switch` (the switch protocol), `nixconvoy-confirmed-switch` (the
  target-side helper) and `nixconvoy-boot-assessment`.
- `tests/switch_test.py`: the switch driven end to end against a fake target,
  the lineage classification, and the git cores on scratch repositories.
- `nix flake check` runs all of it.

## Prior art

nixos-rebuild, deploy-rs and colmena each do things nixconvoy does not,
and nixconvoy exists for the last group, which none of them cover: several checkouts
deploying unmerged work to the same targets, each safe from the others.

### Targets

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| `boot` and `test` goals | ✓ | ✓ | ✓ |   |
| `dry-activate` goal | ✓ | ✓ | ✓ | ✓ |
| Profiles other than the system (home-manager, user) |   | ✓ |   |   |
| nix-darwin targets |   | ✓ |   |   |
| Non-flake configurations | ✓ |   | ✓ |   |
| Node tags and groups |   |   | ✓ | ✓ |
| Run a command on every node |   |   | ✓ | ✓ |
| Upload secrets outside the store |   |   | ✓ |   |
| Prompt for the sudo password | ✓ | ✓ | ✓ |   |

### Building

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Build locally | ✓ | ✓ | ✓ | ✓ |
| Build on a designated builder | ✓ |   |   | ✓ |
| Each target builds its own closures | ✓ | ✓ | ✓ |   |

### Performance

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Parallel switch |   | ✓ | ✓ | ✓ |
| Parallel eval |   |   | ✓ | ✓ |
| Eval skipped when inputs unchanged |   |   |   | ✓ |

### Safety

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Roll back when nixconvoy cannot confirm the switch |   | ✓ |   | ✓ |
| Target-side checks decide the confirmation |   |   |   | ✓ |
| A flake check gates the deploy |   | ✓ |   | ✓ |
| Refuses a switch that would restart protected units |   |   |   | ✓ |
| Switches the router (or any target) last |   |   |   | ✓ |
| Commits and pushes only after the switch succeeded |   |   |   | ✓ |
| Every deployed state is a pushed commit; a branch per target records its deploy history |   |   |   | ✓ |
| Boot counting: an unreachable boot falls back to the last proven generation |   |   |   | ✓ |

### Concurrent checkouts

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Several checkouts deploy unmerged work to the same targets |   |   |   | ✓ |
| Refuses to revert another checkout's in-flight work |   |   |   | ✓ |
| Waits for a deploy under way on the same targets instead of building to be refused |   |   |   | ✓ |

## License

MIT or Apache-2.0, at your option.
