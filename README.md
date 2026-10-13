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
  evaluates and switches nothing.
- **`land` lands a branch on main**, as one commit whose message is the
  branch's `COMMIT_MSG.md`; **`abandon`** discards it.  Either way the
  checkout stays on its work branch, reset onto the new main, and every
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
- **Deploy records.**  Before a target is switched, its deploy record - a
  commit of the exact tree it will run, on its previous record, main, the
  unioned branches and the increment - is pushed to origin, so no target
  ever runs a state origin does not hold.  Once the target runs it,
  `deployed/<target>` points at it: `git log --first-parent
  origin/deployed/<target>` is the target's deploy history, and `git diff`
  between two such branches what two targets run differently.
- **Confirmed switches.**  With the NixOS module, a switch activates the
  new system with `switch-to-configuration test` under a dead-man timer,
  verifies it from the deploying machine (a new ssh connection gets in and
  finds the new system running, its own checks pass, the internet is
  reachable where asked for), and only then sets the profile and bootloader
  entry;
  otherwise the target rolls back to what it ran.  No reboot is ever
  automatic.
- **Protected units.**  A target can declare units a deploy must never
  restart unasked (a router's networkd, nftables, dnsmasq).  The new system
  is dry-activated first; a switch that would touch one stops the whole
  deploy unless `--danger-network-config-change` is given.
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
branch_prefix = "{machine}-deploys"   # {machine}: this machine's hostname; a worktree under
                                      # a plain checkout owns "<prefix>-<name>"

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
```

### The NixOS module

```nix
nixconvoy.confirmedSwitch.deadline = 30;        # seconds before an unconfirmed switch rolls back (activation included)
nixconvoy.confirmedSwitch.internetCheck = true; # the check also wants the internet reachable (default off)
nixconvoy.confirmedSwitch.checks = ''           # bash, as root, errexit: the target's own assertions
  systemctl is-active -q nginx.service
'';
nixconvoy.protectedUnits = [ "systemd-networkd.service" "nftables.service" ];
nixconvoy.switchLast = true;                # the router: switched alone, after everything else
```

`nixconvoy.nixosModules.boot-assessment` adds `nixconvoy.bootAssessment.*`
(requires lanzaboote).

## Commands

```
nixconvoy apply [-m "msg"] [--reset] [target|@tag ...]   deploy
nixconvoy apply --dry-activate [target|@tag ...]   build and copy, then only report what each switch would do
nixconvoy commit -m "msg"      commit + push the staged state, no build or switch
nixconvoy land [name ...]      land the work branch on main
nixconvoy abandon [--apply-now] [name ...]   discard it (--apply-now: and deploy, reverting it now)
nixconvoy prune [name ...]     remove the finished subagents' worktrees holding nothing of their own
nixconvoy pull                 rebase every checkout on this machine onto origin/main
nixconvoy status               every checkout on this machine, and origin's other branches
nixconvoy review               the pending landing: COMMIT_MSG.md, then the diff
nixconvoy exec [target|@tag ...] -- cmd [arg ...]   run a command on each target, concurrently
```

`nixconvoy --help` and the docstring of `src/nixconvoy/cli.py` describe each
step of a deploy in full.

## Worktrees

A git worktree under `<checkout>/.claude/worktrees/<name>` (the layout
Claude Code's worktree isolation creates) owns `<checkout's branch>-<name>`
from its first deploy, deploys in parallel with its parent, and is removed
with its branch by `land` or `abandon` once its task is done - or by
`prune` when it holds nothing of its own and its agent has stopped (a
resume of the agent recreates it) or the session that spawned the agent
has exited.  `integrations/claude-code/` is a Claude Code plugin holding
the hooks that give every subagent spawned inside a repository such a
worktree (`isolation: worktree` added to the Agent call unless it chose
otherwise), create it under the spawning checkout, prune it when its agent
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
