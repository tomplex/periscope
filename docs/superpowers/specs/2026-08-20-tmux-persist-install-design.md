# tmux persistence auto-install

`bin/periscope install` provisions everything continuation-over-reboot needs,
on both sides of the tmux boundary, and `GET /api/healthz` reports whether the
live tmux server actually has it.

## Decisions

**Periscope owns a sourced file, not a block in `~/.tmux.conf`.** Install
regenerates `~/.config/periscope/tmux.conf` wholesale on every `install` /
`update` and adds exactly one line to `~/.tmux.conf`:
`source-file <that path>`. No merge logic over a hand-edited file; `uninstall`
removes that one line and the owned file. TPM follows `source-file` lines when
it scans for `@plugin` declarations (`_sourced_files` in its plugin helpers),
so the plugin list can live in the owned file.

**Ordering rule.** TPM must run after every `@plugin` line. If `~/.tmux.conf`
contains a line matching `run.*tpm/tpm`, the source line goes directly above
it. Otherwise the source line is appended, followed by
`run '~/.tmux/plugins/tpm/tpm'`. The owned file never runs TPM itself.
Idempotent: a second install that finds the source line changes nothing.

**The owned file sets only what continuation requires:**

```
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-resurrect'
set -g @plugin 'tmux-plugins/tmux-continuum'
set -g @continuum-restore 'on'
set -g @resurrect-processes '~claude'
set -g @resurrect-hook-post-save-layout '<REPO>/bin/periscope resurrect-rewrite'
```

`<REPO>` is resolved at install time, like the launchd plist. Because the
source line sits at the bottom (just above `run tpm`), these win over earlier
hand-set values — the direction that fixes a stale hardcoded hook path. Save
interval and pane-contents capture stay the user's; periscope drives the save
cadence itself (`resurrect.save_now`).

**Plugins.** TPM is cloned into `~/.tmux/plugins/tpm` if absent, then TPM's
own non-interactive `bin/install_plugins` clones whatever `@plugin` lines are
missing. TPM reports clone failures only as text, so install verifies the three
plugin directories exist afterwards and fails loudly if any is missing — a
silent install is the bug being fixed. If a tmux server is running,
`tmux source-file ~/.tmux.conf` makes the hook path live immediately;
continuum's auto-restore is gated on server start time, so re-sourcing a live
server never restores over it.

**Healthz reads the LIVE tmux server, not the conf file.** The file being
right and the server having sourced it are different facts.

```
resurrect: {
  ok,                     // every check below passes
  plugins: {tpm, tmux-resurrect, tmux-continuum},   // directory exists
  conf_sourced,           // ~/.tmux.conf carries the source line
  restore_on,             // live @continuum-restore == on
  processes_ok,           // live @resurrect-processes contains ~claude
  hook_current,           // live hook == THIS checkout's bin/periscope resurrect-rewrite
  last_save_at,           // mtime of the resurrect `last` target, or null
  last_save_stale         // a save exists and is older than 2× the live save interval
}
```

No tmux server → the live checks are false and `ok` is false. No save yet
(fresh install) → `last_save_at: null`, not stale — the first save lands within
one interval. A stale save flips `ok`: that is the 24-day-gap failure made
visible.

## Mechanics

- `periscope/tmux_persist.py` — stdlib + `periscope.config` only (runs under
  plain `python3` from the shell verb, same discipline as `resurrect.py`).
  `install(repo)`, `uninstall()`, `status(repo)`; `python -m` entry point.
- `bin/periscope install-tmux` / `uninstall-tmux`; called from `install`,
  `update`, `uninstall`. `uninstall` leaves the plugins and any `run tpm` line
  in place — removing TPM is not periscope's to do.
- `routes/healthz.py` adds the `resurrect` key from `tmux_persist.status`.
- `tests/test_tmux_persist.py` covers the conf-line rules (insert above TPM,
  append with TPM, idempotent, uninstall round-trip) and `status` against
  stubbed `tmux show-options` output. Cloning and live re-source are verified
  by hand.
