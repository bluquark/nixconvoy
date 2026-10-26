# nixconvoy

Deploy a NixOS flake to a few targets from several checkouts at once, without
one checkout ever reverting what another already switched.

nixconvoy is for the way a homelab actually evolves: lots of small,
experimental changes, tested in production, kept honest by NixOS's atomic
switches and generations.  You stage a change, run `nixconvoy deploy`, and the
targets that differ from the result are switched.  If the target does not come
back reachable and healthy, the switch rolls itself back.  Only after the
targets run the change is it committed, so a branch never claims more than
what is deployed.

## The model in git terms

- **A work branch is where experiments live.**  Each checkout you deploy from
  owns one, listed by prefix in `nixconvoy.toml`'s `work_branches`.  A
  deploy commits the staged state onto it and pushes it; nothing else
  writes there: a deploy, land or undeploy that finds origin's copy
  holding commits this checkout never pushed is refused before it
  switches, pushes or resets anything (a writer arriving while a deploy
  runs leaves the branch unpushed, the deploy failing at its end).
- **The targets run the convoy: origin/main plus every open work branch.**
  Each deploy union-merges the pushed work branches (mergiraf resolves
  structural conflicts) and builds that.  Two checkouts changing unrelated
  modules both stay deployed; a real conflict stops the deploy and names
  the branch.
- **Targets are inferred.**  Each target's ground truth is its running
  system.  Whatever differs from the union is switched; a no-op deploy
  evaluates and switches nothing.  An increment that changes nothing any
  target's evaluation reads (docs, dotfiles) is committed and pushed
  without a deploy: the probes' results are dropped and nothing is built
  or switched, so nothing need be reachable; the targets' drift waits for the next deploy reaching one,
  or for a deploy naming targets.
- **`land` lands a branch on main**, as one commit whose message is the
  branch's `COMMIT_MSG.md` (or `-m <msg>`, for a quick landing of a
  branch without one: both at once is refused), opened in git's editor
  first when `land` runs on a terminal without `-m`, so a message an agent
  wrote can be adjusted by hand before it lands (an agent's own shell is no
  terminal: there the file lands as it is); **`undeploy --back-to origin/main`** discards it.  Either way the
  checkout stays on its work branch, reset onto the new main (a subagent
  worktree too: `close` removes one), and every
  other checkout on the machine is rebased onto it.  A landing touches no
  target; `undeploy` runs a deploy at once, reverting the work on the targets
  running it - **`undeploy`** alone undoes the branch's latest increment,
  `--back-to <rev>` every increment back to that one, the branch reset
  there (an increment reset off its branch by hand stays deployed until
  the next deploy by anyone, which says what it reverts).

A checkout on `main` (or any branch `work_branches` does not list) is a
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
  deletes the announcement (a main checkout's is cleared by the push of
  main that lands its HEAD, by its owner, and swept by the next deploy);
  a deploy ending without a switch withdraws it.
- **Provenance.**  Every target records where its system came from: the union,
  each branch's tip, and the machine and checkout that deployed it.  A
  deploy that would overwrite another checkout's not-yet-pushed increment
  is refused (or leaves that target alone when its own work does not reach
  it) until that deploy is over: its announcement cleared without the push,
  which only the deploy's own end or a hand clear does.  Then the increment
  is abandoned and taken over with a warning.  A deploy that died leaves
  its announcement up, so its targets stay refused - the refusal names the
  dead deploy and the clear that ends it - and nothing is ever taken over
  on a clock.  Work of a branch `work_branches` does not list stays
  refused until listed.
- **Held-back targets.**  An increment deployed to named targets alone is
  committed with the targets it reaches (`Reaches:`) and the ones now
  running the branch's tip (`Deployed:`); a target in the first and not the
  second is held back until a deploy from that checkout switches it.  The
  union carries the work, so every other work branch's deploy leaves a held
  target alone, or is refused when its own work reaches it; a main
  checkout's deploy (a nightly's) leaves it behind rather than fail; and
  `land` refuses without `--not-yet-deployed` - no restricted deploy rolls
  out elsewhere by accident.
- **Deploy records.**  Before a target is switched, its deploy record - a
  commit of the exact tree it will run, on its previous record, main, the
  unioned branches and the increment - reaches origin as a parent of the
  deploy's announcement, so no target ever runs a state origin does not
  hold (a deploy with nothing pending announces once it has targets to
  switch, for this alone).  Once the target runs it, the one atomic push
  that ends the deploy moves the work branch, points
  `nixconvoy-history/<target>` at the record and deletes the announcement
  (a main checkout's is settled instead, cleared by the owner's push of
  main): `git log --first-parent
  origin/nixconvoy-history/<target>` is the target's deploy history, and
  `git diff` between two such branches what two targets run differently.
- **Confirmed switches.**  With the NixOS module, a switch activates the
  new system with `switch-to-configuration test` under a dead-man timer,
  verifies it from the deploying machine (a new ssh connection - a direct
  read on a local target - finds the new system running, its own checks
  pass, the internet is reachable where asked for), and only then sets the
  profile and bootloader entry; otherwise the target rolls back to what it
  ran.  No reboot is ever
  automatic.
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

Then, from the repo root, write its settings and deploy:

```sh
nix run .#nixconvoy -- init
nix run .#nixconvoy -- deploy -m "describe this increment"
```

`init` detects what it can (the flake directory, the hosts directory, the
targets' system) and writes `nixconvoy.toml` - its optional sections as
commented examples, the branch prefix `nixconvoy/`, listed as the one
work-branch prefix (`nixconvoy/{machine}/`, the hostname, namespaces each
machine's branches when several deploy); the ssh address
template starts as `<you>@{target}`, for you to edit.

On a NixOS machine you deploy from, the package in
`environment.systemPackages` puts `nixconvoy` (and `git review`) on PATH.
That is the running system's build, which trails the lock until the
machine itself switches; an unattended deploy that first moves the lock
should run the lock's build (`nix run .#nixconvoy -- deploy ...`).

Requirements on each target: `/run/current-system` reachable over ssh with
passwordless sudo for the deploying user (a switch, the provenance file under
`/var/lib/nixconvoy` and a dry activation all need root), and the target's
nixosConfiguration named after its directory under `hosts/`.

## Layout nixconvoy expects

```
nixconvoy.toml      the settings (below), written by `nixconvoy init`
COMMIT_MSG.md       on a work branch: the pending landing's commit message
<flake>/flake.nix
<flake>/hosts/<name>/   one directory per nixosConfiguration
```

### nixconvoy.toml

`nixconvoy init [<flake-dir>]` writes it, refusing to overwrite one.  The keys up to `[eval]` are required and have
no defaults - init detects each, the ssh address template starting as
`<you>@{target}`.  The sections after are optional: absent means none.

```toml
config_version = 1
flake_dir = "nixos"               # directory holding flake.nix
targets_dir = "nixos/hosts"       # one directory per nixosConfiguration: the targets
targets_arch = "x86_64-linux"     # the targets' system
ssh_address_template = "me@{target}.lan"   # a target's ssh address; the store host's too
branch_prefix = "nixconvoy/"       # a checkout owns "<prefix><leaf>" (`open <path>`: the path's name), a
                                   # subagent worktree "<checkout's branch>-<name>" ("<prefix>main-<name>"
                                   # under a plain checkout); "{machine}" in it is this machine's hostname,
                                   # a namespace per machine ("nixconvoy/{machine}/")
work_branches = ["nixconvoy/"]     # the work branches nixconvoy may union or own, by the prefixes of
                                   # their names; a pushed branch starting with none is ignored
child_worktrees = ".claude/worktrees"   # where `open --child <worktree>` puts a child (subagent) worktree, under the
                                        # checkout (Claude Code's own place; excluded through .git/info/exclude)

[eval]
parallel = "auto"            # Determinate Nix's parallel evaluation: true, false, or "auto" (on when nix has it)
precheck = "repo-lint"       # optional: checks.<targets_arch>.<name>, a flake check proven before any build
                             # (a pass memoized on the union's tree)

[build]
store = { "*" = "bigbox", laptop = "laptop" }   # realise closures in that machine's store, by the hostname
                                     # nixconvoy runs on ("*": any other); a machine mapped to itself
                                     # realises locally.  Where they build is that store's nix daemon's
                                     # call: its own remote builders (nix.conf's builders) or itself
skip_substituters = ["https://slow.cachix.org"]   # left out of deploy builds; the caches are the store host's nix.conf's

[targets.laptop]
ssh_address = "root@10.0.0.9"   # overrides the template
tags = ["portable"]          # `@portable` on the command line names every target tagged so

[submodules.mytool]          # a flake input developed in place, a git submodule (see Submodules)
repo = "~/projects/mytool"   # its repository's checkout here; its origin is the .gitmodules url
pin_file = "pins/mytool.json"  # optional: the pinned rev and its committer date, kept beside the gitlink
```

### The NixOS module

```nix
nixconvoy.confirmedSwitch.deadline = 30;        # seconds before an unconfirmed switch rolls back (activation included)
nixconvoy.confirmedSwitch.internetCheck = true; # the check also wants the internet reachable (default off)
nixconvoy.confirmedSwitch.checks = ''           # bash, as root, errexit: the target's own assertions
  systemctl is-active -q nginx.service
'';
nixconvoy.switchLast = true;                # the router: switched alone, after everything else
```

`nixconvoy.nixosModules.boot-assessment` adds `nixconvoy.bootAssessment.*`
(requires lanzaboote).

### The eval memo

A deploy evaluates a target only when something its evaluation can read
changed: each target is keyed on `flake.nix`, its `hosts/<name>/`,
transitively every repo path a scanned `.nix` file names by path literal,
and the lock entries of the flake inputs those files mention.  The scan is
lexical, so the flake keeps two habits for the memo to hit:

- **Import files, not directories.**  A directory named by a path
  (`import ./pkgs`, a fileset rooted at `../.`, a `readDir`) counts whole,
  and one that holds the flake directory - the repository root included -
  keys the target on the whole tree; a deploy says so, with the file that
  names it.  The precheck's stanza in `flake.nix`
  (`<check> = import ./lint.nix { ... };`) is skipped: no target's
  evaluation reads the check.
- **Reach the repo's own packages by import at the use site**
  (`import ../pkgs/foo.nix { inherit pkgs; }`), not through an overlay or
  another attribute set: an attribute's laziness is invisible to the scan,
  so an overlay applied to every host keys every host on every file it
  names.

`specialArgs` must be a flat `{ inherit a b; }` of inputs: `self` or
`inputs` handed to the modules can reach anything, and keys every target
on the whole tree.

A target's .drv is trusted only while it is a GC root
(`~/.cache/nixconvoy/eval-memo/roots/`): a deploy roots each fresh
evaluation's as soon as it returns and roots a memo entry's again
before the hit counts, so the store's own collector (determinate-nixd
runs one on a schedule and at the daemon's start, a switch's included)
cannot take it before the build; one taken anyway is re-evaluated.  Each
checkout records which entries its last deploy used (`users/`), and a
deploy's prune unroots the entries no record names - a landing or the
nightly lock update moves every checkout's keys - so the roots pin no
tree nobody deploys; an unrooted entry stays a hint until the collector
takes its .drv.  The precheck's passes are proofs about trees, not
derivations: one line each (the check, the tree, when, which checkout,
the check's out and drv) appended to `eval-memo/prechecks.jsonl`, kept
for good.

## Commands

```
# Setting up a repository
nixconvoy init [<flake-dir>]   write nixconvoy.toml for a fresh install: the flake, hosts directory and system detected, the ssh address template as this user

# Managing worktrees
nixconvoy close [<path> ... | <worktree> ... | <submodule> ...] [--force]   remove agent checkouts by path (with their work branch), subagent worktrees, or submodule worktrees (with their branch), holding nothing of their own (--force: whatever they hold)
nixconvoy pull                 rebase every checkout on this machine onto origin/main
nixconvoy status               every checkout on this machine, and origin's other branches
nixconvoy hook <event>         the Claude Code plugin's hook of that event (session-start, subagent-stop, worktree-create, worktree-remove, worktree-resume), run by its hooks/run.sh <event> with the hook's input on stdin
nixconvoy open <path> [--branch <work-branch>]   an agent checkout at that path on a work branch of this machine's, named after the path (refused when origin holds it: --branch takes it over or names another)
nixconvoy open --child <worktree>   a child (subagent) worktree <checkout>/<child_worktrees>/<worktree> (what the plugin's create hook makes)
nixconvoy open --submodule <input>   the flake input developed in place under this checkout (see Submodules)

# Deploying and committing changes
nixconvoy deploy [<target> | @<tag> ...] [-m <msg>] [--evict] [--dry-activate]   deploy the staged increment (-m: its message, default "<checkout>: increment <n>"), or with nothing staged converge the targets on the union as it is; --dry-activate: build and copy, then only report what each switch would do
nixconvoy undeploy [<worktree> ... | --with-children] [--back-to <rev>]   undo the increments from HEAD back to <rev> (default HEAD^, the latest one; origin/main: the whole branch): the branch reset there, then a deploy reverting them on the targets (--with-children: every subagent worktree holding increments, then this checkout, one deploy)
nixconvoy land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]   land the work branch on main (--with-children: this checkout and its subagent worktrees as one unit, one main commit with the subs' COMMIT_MSG.md appended; -m: under <msg>, for a branch without COMMIT_MSG.md, else the message opens in git's editor on a terminal; --not-yet-deployed: targets its increments held back included)
nixconvoy review [<worktree> | <submodule> | <branch> ... | --with-children] [<diff-arg> ...]   the pending landing: COMMIT_MSG.md, then the diff (--with-children: the checkout's and its subagent worktrees' together)

```

A `<worktree>` is a subagent worktree's name (a unique prefix will do) or
a branch a worktree on this machine is on: `land` and `undeploy` act in that
worktree, so a branch with none here is refused, while `review`, which only
reads, takes any `<branch>` (against its merge-base) and a `<submodule>`'s
name too.

`nixconvoy --help` and the docstring of `src/nixconvoy/cli.py` describe each
step of a deploy in full.

## Child worktrees

A git worktree nested in a checkout - `<checkout>/<child_worktrees>/<name>`,
the directory nixconvoy.toml names (`.claude/worktrees/`, where Claude
Code puts its own, in a repository that is no nixconvoy repository; `open` writes it into `.git/info/exclude` unless
it is ignored already), where `nixconvoy open --child <worktree>` makes one
and the create hook below runs it - is that checkout's child, or subagent,
worktree: it owns `<checkout's
branch>-<name>` from its first deploy and deploys in parallel with its
parent.  `land` and `undeploy --back-to origin/main` leave it in place like any checkout, idle on
its reset branch; `nixconvoy close <worktree>` removes it when it holds
nothing of its own (`--force`: whatever it holds), as the plugin's hooks do
on their own once its agent has stopped (a resume of the agent recreates it)
or the session that spawned the agent has exited (`nixconvoy hook
subagent-stop` and `hook session-start`, `nixconvoy --help`: Hook).  When the task
worktrees each did a piece of their parent's task, `land --with-children` merges
their branches into the parent's tree and lands it all as one commit
under the parent's COMMIT_MSG.md with each sub's appended (`review
--with-children` shows each,
`undeploy --with-children --back-to origin/main` discards each).  `nixconvoy open <path>` makes a
further agent checkout beside the others instead - a worktree at that
path on a listed work branch of this machine's, named after the path
(when origin already holds that branch, a removed checkout's or another
machine's, `--branch` says whether to take it over or open another) - and
`close <path>` removes one holding nothing of its own.
`integrations/claude-code/` is a Claude Code plugin holding the hooks
that create an isolated subagent's worktree under the spawning checkout,
close it when its agent stops and recreate it for its resume, beside
`bin/git-review` (which the package installs as `git review`) and
`prompt.bash`, a bash prompt segment to source from a copy of this
repository (the package does not ship it); `integrations/claude-code-isolate/`
is a second plugin whose one hook gives every subagent spawned inside a
repository such a worktree (`isolation: worktree` added to the Agent
call unless it chose otherwise):

```
claude plugin marketplace add bluquark/nixconvoy
claude plugin install nixconvoy@nixconvoy --scope project
claude plugin install nixconvoy-isolate@nixconvoy --scope project
```

The isolation is a plugin of its own because two hooks that each rewrite
one Agent call race: Claude Code runs matching hooks in parallel and
keeps one `updatedInput`, whichever lands last.  A PreToolUse hook of
your own on `Agent` that rewrites the call (pinning a subagent's model,
say) must be the one rewriter: leave `nixconvoy-isolate` uninstalled and
add `isolation: "worktree"` from inside it (`subagent-isolate.sh` shows
the rule: a call without `isolation`, spawned in a git repository - the
session's project directory, not the shell's, which drifts).  Both hooks
return the rewritten call as allowed, so an `ask` rule on Agent does not
prompt for a spawn they isolated.

`--scope project`, run in the repository, writes the enablement into its
tracked `.claude/settings.json`: every worktree of it has it, and each
collaborator runs the install once.  The hooks belong to a repository
with `nixconvoy.toml`: at user scope they run in every repository you
open, where the create hook still makes a subagent's worktree (a plain
`.claude/worktrees/<name>` on `worktree-<name>`) but the prunes, which
read the work branches, leave it to `git worktree remove`.  `nixconvoy`
must be on PATH where the hooks run (each is `hooks/run.sh <event>`, which runs
`nixconvoy hook <event>`).

## Submodules

A flake input you also develop - a tool of your own, a fork - gets the
same deploy model, without a separate checkout per task.  Make it a git
submodule named after the input (`git submodule add --name <input> <url>
<path>`), take it in flake.nix as a relative path input
(`inputs.<input>.url = "path:./<path from the flake directory>"`, so
flake.lock names no rev: the gitlink in the tree is the pin) with
`inputs.self.submodules = true`, and name its repository's checkout on
this machine in `nixconvoy.toml` (`[submodules.<input>] repo = "..."`;
its origin must be the .gitmodules url).  nix reads a committed tree's
submodules from that url at the pinned rev, so the rev must be pushed,
and a checkout with uncommitted changes' from their directories, so a
hand `nix` command there wants every submodule planted or the committed
tree named (`git+file://<checkout>?rev=<HEAD>&dir=<flake dir>`); a
deploy evaluates a clean worktree of the staged snapshot and needs
neither.  A submodule's own flake gaining an input wants `nix flake
lock` likewise, its inputs being locked in the parent's lock file.
Never `git submodule update`: that clones a second copy under
.git/modules, which a worktree's removal deletes with any unpushed
commit.  From a checkout on a work branch:

- **`open --submodule <input>`** plants a worktree of the repository at
  the submodule's path, on a branch named after the checkout's work
  branch, off the default branch; stage there what is ready to deploy.
  A checkout that never opens it keeps the directory empty, as git
  leaves an unpopulated submodule.
- **A deploy carries it as a tree change.**  `deploy` commits the
  worktree's staged state onto its branch with the `-m` message, pushes
  it and stages the gitlink on the pushed rev, which the union, the eval
  memo and the provenance then see like any edit.  Two work branches moving
  the same submodule meet in the union as a gitlink conflict: the
  branches are merged in the repository (its configured checkout, else a
  bare mirror under `~/.cache/nixconvoy/submodules/`; mergiraf on the
  conflicts), the merge pushed to the origin's
  `refs/nixconvoy/union/<input>/<sha>` and pinned there - deterministic,
  the same again on a retry, and kept; branches that conflict are a
  union conflict like any other.  Once one lands, every other checkout's
  catch-up (a deploy, `pull`, the landing itself) brings its worktree
  onto the landed default branch as it brings the checkout onto main -
  fast-forwarded when it has nothing of its own, rebased with its staged
  and unstaged state otherwise, pushed and pinned - each of the checkout's
  replayed commits on the rebased counterpart of its own pin, so every
  increment keeps the pin move it made.  That rebase replays
  only what no landing has squashed yet, and goes through the conflicts
  mergiraf solves; one still not clean is reported and left as it was,
  to resolve by hand.  A
  deploy refuses to pin a submodule still behind that branch, and a
  worktree reset below the work the tree deploys (`close <input>
  --force` drops it deliberately).
- **`land` lands the submodule with main:** its tip merged onto the default
  branch as the union merges it and squashed there as one commit (the
  `COMMIT_MSG.md` in the worktree, tracked or not, else the checkout's),
  that commit pinned as a commit of the work branch and pushed under a
  temporary ref, then main, then the default branch moved onto the commit
  - the push of main, the one that can be refused, goes first, and refused
  it leaves every default branch as it was and the pins off the work
  branch; a submodule whose HEAD is not what the tree pins is refused.  `undeploy`
  resets it onto the pin of the rev the checkout goes back to (its
  default branch, for a landed pin).  Either leaves the worktree in
  place.  `close <input>`
  removes the worktree and its branch, here and on its origin,
  refusing what would lose work unless `--force` (the pin then put back
  to origin/main's, staged for the next deploy); the directory stays,
  empty; a subagent worktree's submodules go with it.  A tip a force push or
  removal leaves behind stays under `refs/nixconvoy/past/<branch>/<sha>`
  while a pushed work branch or deploy record pins it (nix fetches a rev
  only while a ref holds it); a landing deletes the rest.
- **`pin_file` keeps the pin readable.**  A flake cannot see a
  submodule's gitlink, nor its history: the tree it gets is the
  submodule's files.  With `[submodules.<input>] pin_file = "<path>"`,
  every pin nixconvoy sets also writes that file, staged beside the
  gitlink, as JSON: `{"committed": <its committer date, seconds since the
  epoch>, "rev": "<sha>"}` - a version number that rises across rebases,
  union merges (dated a second past their parents') and squash landings,
  or the rev itself, for the flake to read
  (`builtins.fromJSON (builtins.readFile ./<path>)`).  A union or rebase
  that resolves a gitlink rewrites the file with it; a deploy rewrites a
  file a hand-moved gitlink left behind (a main checkout only says so).
- **Never work in the repository's own checkout.**  `status` flags
  edits there, and the `nixconvoy-submodule` marker nixconvoy leaves in
  its git dir lets a global pre-commit hook refuse an agent's commit on
  the default branch there (`[ -f "$(git rev-parse
  --git-common-dir)/nixconvoy-submodule" ]` with `git rev-parse
  --git-dir` the common dir; a repository setting its own
  `core.hooksPath`, husky's say, bypasses global hooks).

## How it is built

- `src/nixconvoy/`: the Python driver, which performs git, nix and ssh.
- `switch-guard/`: a Rust crate holding the decisions as Sans-I/O state
  machines, with stateright models and property tests: `nixconvoy-plan` (the
  rebase, the union, the drift verdicts, a subagent worktree's pruning
  against the create hook, the sessions and its agent's stops and
  resumes), `nixconvoy-switch` (the switch protocol), `nixconvoy-confirmed-switch` (the
  target-side helper) and `nixconvoy-boot-assessment`.
- `tests/switch_test.py`: the switch driven end to end against a fake target,
  the provenance classification, and the git cores on scratch repositories.
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
| Build on a designated machine | ✓ |   |   | ✓ |
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
