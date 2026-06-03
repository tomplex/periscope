#!/usr/bin/env bash
# Diagnostic for periscope gripe #4 (garbled/repeated Ink markdown).
#
# Question: does Claude's RAW output stream replay cleanly, or is the
# corruption already in the bytes?
#   - raw replays clean  -> tmux's GRID is the corruptor (capture-pane snapshot
#                           + Ghostty-direct both read that grid). Option B fixes it.
#   - raw garbles too    -> deeper (Ink<->emulator state). B alone won't fix #4.
#
# pipe-pane taps the program's raw output bytes (what a single emulator like
# Warp would see), BEFORE tmux re-renders them into its own grid.
#
# Usage:
#   diag/term-capture.sh panes              # list candidate Claude panes
#   diag/term-capture.sh start <target>     # begin raw recording on a pane
#   diag/term-capture.sh mark  <target>     # snapshot tmux's grid at a garble moment
#   diag/term-capture.sh stop  <target>     # stop recording
#   diag/term-capture.sh replay <target>    # how to replay the raw bytes cleanly
#
# <target> is tmux's "session:window.pane", e.g. "tc/foo:1.0".

set -euo pipefail

ROOT=/tmp/pdiag
sanitize() { printf '%s' "$1" | tr '/:.' '___'; }
dir_for() { echo "$ROOT/$(sanitize "$1")"; }

cmd=${1:-help}
target=${2:-}

case "$cmd" in
  panes)
    echo "session:window.pane | width x height | command"
    tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} | #{pane_width}x#{pane_height} | #{pane_current_command}'
    ;;

  start)
    [ -n "$target" ] || { echo "need <target> (see: $0 panes)"; exit 1; }
    d=$(dir_for "$target"); mkdir -p "$d"
    : > "$d/raw.bin"
    tmux display-message -t "$target" -p '#{pane_width}x#{pane_height}' > "$d/size.txt"
    # -O: capture output only. Appends raw program output (Claude's bytes).
    tmux pipe-pane -O -t "$target" "cat >> $d/raw.bin"
    echo "recording raw output of $target -> $d/raw.bin (size $(cat "$d/size.txt"))"
    echo "now drive Claude until the garble appears (try: 'render a big markdown"
    echo "table plus a long nested bullet list'), then: $0 mark $target"
    ;;

  mark)
    [ -n "$target" ] || { echo "need <target>"; exit 1; }
    d=$(dir_for "$target"); mkdir -p "$d"
    # tmux's rendered grid — the thing periscope's capture-pane snapshot and
    # Ghostty-direct both display. If THIS shows the garble but raw replays
    # clean, the grid is the culprit.
    tmux capture-pane -t "$target" -p -e -S -200 > "$d/grid.txt"
    tmux display-message -t "$target" -p '#{pane_width}x#{pane_height}' > "$d/size.txt"
    echo "grid snapshot -> $d/grid.txt (open it; does it show the garble?)"
    ;;

  stop)
    [ -n "$target" ] || { echo "need <target>"; exit 1; }
    tmux pipe-pane -t "$target"   # no command = stop
    d=$(dir_for "$target")
    echo "stopped. raw bytes: $d/raw.bin   grid: $d/grid.txt"
    ;;

  replay)
    [ -n "$target" ] || { echo "need <target>"; exit 1; }
    d=$(dir_for "$target")
    size=$(cat "$d/size.txt" 2>/dev/null || echo "?x?")
    cols=${size%x*}; rows=${size#*x}
    echo "Raw bytes were emitted at ${size}. Replay them in a SINGLE clean"
    echo "emulator (no tmux) sized to match, and compare to grid.txt:"
    echo
    echo "  1. open a fresh Ghostty window, resize to ${cols}x${rows}:"
    echo "       printf '\\e[8;${rows};${cols}t'"
    echo "  2. replay the raw stream:"
    echo "       cat $d/raw.bin"
    echo
    echo "  clean here + garbled in grid.txt  => tmux grid is the corruptor (Option B fixes #4)"
    echo "  garbled here too                  => corruption is in the raw bytes (deeper)"
    ;;

  *)
    sed -n '2,30p' "$0"
    ;;
esac
