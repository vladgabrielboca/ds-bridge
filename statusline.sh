#!/usr/bin/env bash
# Claude Code status line: show ds-bridge progress.
# Needs only jq. Ignores stdin.
cat >/dev/null 2>&1

P="$HOME/.local/share/ds-bridge/progress.json"
[ -f "$P" ] || exit 0

data=$(jq -r '[(.state // ""), (.job // ""), (.round // 0), (.steps // 0),
               (.tools // 0), (.out_tokens // 0), ((.started // 0) | floor),
               ((.updated // 0) | floor), (.last // ""), ((.secs // 0) | floor),
               (.tok_s // "")]
              | map(tostring) | @tsv' "$P" 2>/dev/null) || exit 0
[ -n "$data" ] || exit 0

IFS=$'\t' read -r state job round steps tools out_tokens started updated last secs tok_s <<< "$data"

now=$(date +%s)

fmt_tok() {
    local n=$1
    if [ "$n" -ge 1000 ]; then
        printf '%d.%dk' $((n / 1000)) $(((n % 1000) / 100))
    else
        printf '%d' "$n"
    fi
}

fmt_secs() {
    local s=$1
    [ "$s" -lt 0 ] && s=0
    printf '%d:%02d' $((s / 60)) $((s % 60))
}

tok_s_seg=""
case "$tok_s" in
    ''|*[!0-9]*) ;;
    *) tok_s_seg=" · ${tok_s} tok/s" ;;
esac

case "$state" in
    running)
        age=$((now - updated))
        if [ "$age" -gt 1800 ]; then
            printf 'DeepSeek %s stalled?\n' "$job"
            exit 0
        fi
        spinners=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
        sp=${spinners[$((now % 10))]}
        printf '%s DeepSeek %s r%s · %s · step %s · %s tools · %s tok%s · ▸ %s\n' \
            "$sp" "$job" "$round" "$(fmt_secs $((now - started)))" \
            "$steps" "$tools" "$(fmt_tok "$out_tokens")" "$tok_s_seg" "$last"
        ;;
    done)
        if [ $((now - updated)) -le 120 ]; then
            printf '✓ DeepSeek %s done in %s%s\n' "$job" "$(fmt_secs "$secs")" "$tok_s_seg"
        fi
        ;;
    error)
        if [ $((now - updated)) -le 120 ]; then
            printf '✗ DeepSeek %s failed in %s\n' "$job" "$(fmt_secs "$secs")"
        fi
        ;;
esac
exit 0
