# ADR-0026: The laptop reaches the module over SSH
Status: accepted
Date: 2026-11-12

## Context

The module runs in the deployment's worker container, its files in the workspace's
uploads layout (ADR-0024). The statement archive, the edited inputs, a state export
and the key files start on a laptop. The container's name changes with every deploy. It runs
as root, so a hand copy with `docker cp` keeps the source's owner and mode, and a key
file copied that way is either refused by the module or readable by others. The
owner's file names are free text: some start with a dash, hold spaces or shell
characters. A copy cut part way must not leave a half file under a name the archive
walk reads, and a copy must never change the laptop's files.

## Decision

- **One CLI, five commands.** On the laptop, `finance remote push`, `pull` and `exec`.
  In the container, `finance remote receive` and `send-packs`, which push and pull run
  there. No new console script, setting, operation, job or dependency.
- **A private config file, no default.** `--config FILE` or `$FINANCE_REMOTE_CONFIG`
  names one TOML file with the ssh destination, the worker's two compose labels, the
  workspace id and an optional docker command. It is read only when it is a regular
  file of its owner's alone and not a link, and every value is checked against a
  narrow pattern (no leading dash, no space). Hosts and users never live in code.
- **The container by its labels, every call.** `docker ps` filtered on the compose
  project and service must list exactly one running id, else `container_not_found`,
  `container_ambiguous` or `remote_reply_invalid`. A second session then runs `docker
  exec`; a container replaced in between fails closed as a docker error.
- **One quoted remote command.** Every process starts from an argument list. The
  remote command is one argument, `shlex.join` of fixed or checked words, so the
  remote shell sees only single-quoted words; `--` always comes before the
  destination. The remote account's shell must be sh-compatible.
- **Names travel in the stream, never on a command line.** A push or pull is a framed
  stream on stdin or stdout: a magic line, then `F <size> <path>`, the bytes and
  `H <sha256>` per file, then `E <count>`; `X <code>` lets a sender give up between
  files. Line frames rather than tar: five record types are easier to validate whole
  than tar's links, devices and long-name extensions.
- **One path rule on both ends.** UTF-8, at most 1024 bytes, components of 1 to 255
  bytes; no control character, line or paragraph separator, backslash, empty
  component or component starting with a dot; bytes kept as they are. A push may name
  `archive/**`, `inbox/**`, `data/settings.toml`, `data/identities.toml`,
  `data/manual/**`, `data/transfer/**` and `keys/<name>`; a pull only `packs/**`.
  `db.env`, `dumps/` and `logs/` cannot be named, so they never cross.
- **Nothing before the commit, each file whole.** The receiver stages each file under
  a numbered name in a hidden directory beside the areas, checks its hash, then checks
  every target before any is written: a parent reached through a link or outside its
  area is `target_outside_layout`, an existing target that is not a regular file is
  `target_not_regular`, and different bytes are `target_differs` with the count. A
  stream cut or refused up to here writes nothing. Only then are files hard-linked
  into place, each atomically, which never replaces a file that appeared meanwhile
  (`target_appeared`). A refusal during that commit step, whatever its code, keeps
  the files already linked and names how many: the receiver counts each file as it
  lands and puts the count on the refusal. Identical files are skipped. A lock lets one run at a
  time and removes a killed run's leftovers. Files are 0600, made directories 0700;
  modification times are not carried.
- **Refused by default, with one overwrite rule.** `--replace` may replace
  `data/settings.toml`, `data/identities.toml` and `data/manual/**`, the owner's
  edited inputs, so a push can replace `finance init-data`'s templates. It never
  replaces a statement, an inbox file, a key or a transfer file, and a pull never
  replaces anything.
- **Keys only on purpose.** `--keys DIR` alone carries secrets: top-level files, each
  readable by its owner alone (`key_mode_refused`), written 0600 and never replaced
  (rotate by a new name). A routine push without the flag never carries one.
- **Fixed codes on the laptop.** Push and pull drop ssh's own diagnostics and read the
  reply against a closed grammar (`OK <new> <identical> <replaced>` or `NO <code>
  <n>`). Every message is a code and counts: no host, path or file name. `exec`
  passes the remote command's output and status through, and asks for a terminal only
  when its stdin is one, so `finance sink approve` keeps its own terminal check.
- **`finance state import --pushed`.** The export a push put in `data/transfer` sits
  under a container path the laptop cannot know, so import reads it from the bound
  layout.

## Consequences

- The data move runs: install and enable, `finance init-data`, push with `--replace`,
  `state import --pushed`, `rebuild`. Pushing before init-data is refused
  (`finance_layout_missing`).
- A changed archive document, inbox file, key or transfer file is refused, not merged;
  pulling a regenerated pack into the same directory is the owner's own step.
- Each command takes two ssh sessions; an owner on password authentication types
  twice unless ssh's connection sharing is set up.
- An authentication failure reads only `ssh_failed` on push and pull; `exec` or plain
  ssh shows ssh's own message.
- A push changes the inputs' modification times, so the next derive rebuilds; nothing
  keys on them.
- Only `cli.main` imports `cli.remote`, and only `cli.remote` imports `remote.ssh`, the
  one module of these that starts a process: no job, sink or model step can open ssh.

This extends ADR-0024's layout and supersedes nothing.
