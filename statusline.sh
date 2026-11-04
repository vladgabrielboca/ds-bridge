#!/usr/bin/env bash
# Claude Code status line: show ds-bridge progress for all jobs.
# Needs only jq. Ignores stdin.
cat >/dev/null 2>&1

P="$HOME/.local/share/ds-bridge/progress"
[ -d "$P" ] || exit 0

shopt -s nullglob
files=("$P"/*.json)
[ ${#files[@]} -gt 0 ] || exit 0

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

records=()
running=0
for f in "${files[@]}"; do
    data=$(jq -r '[(.state // ""), (.job // ""), (.round // 0), (.steps // 0),
                   (.tools // 0), (.out_tokens // 0), ((.started // 0) | floor),
                   ((.updated // 0) | floor), (.last // ""), ((.secs // 0) | floor),
                   (.tok_s // "")]
                  | map(tostring) | join("\u001f")' "$f" 2>/dev/null) || continue
    [ -n "$data" ] || continue
    IFS=$'\x1f' read -r state job round steps tools out_tokens started updated last secs tok_s <<< "$data"
    last=${last//$'\t'/ }
    last=${last//$'\n'/ }

    tok_s_seg=""
    case "$tok_s" in
        ''|*[!0-9]*) ;;
        *) tok_s_seg=" · ${tok_s} tok/s" ;;
    esac

    is_running=0
    case "$state" in
        running)
            age=$((now - updated))
            if [ "$age" -gt 1800 ]; then
                line=$(printf 'DeepSeek %s stalled?' "$job")
            else
                spinners=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
                sp=${spinners[$((now % 10))]}
                line=$(printf '%s DeepSeek %s r%s · %s · step %s · %s tools · %s tok%s · ▸ %s' \
                    "$sp" "$job" "$round" "$(fmt_secs $((now - started)))" \
                    "$steps" "$tools" "$(fmt_tok "$out_tokens")" "$tok_s_seg" "$last")
            fi
            is_running=1
            running=$((running + 1))
            ;;
        done)
            [ $((now - updated)) -le 120 ] || continue
            line=$(printf '✓ DeepSeek %s done in %s%s' "$job" "$(fmt_secs "$secs")" "$tok_s_seg")
            ;;
        error)
            [ $((now - updated)) -le 120 ] || continue
            line=$(printf '✗ DeepSeek %s failed in %s' "$job" "$(fmt_secs "$secs")")
            ;;
        *)
            continue
            ;;
    esac

    records+=("$started"$'\t'"$is_running"$'\t'"$line")
done

[ ${#records[@]} -gt 0 ] || exit 0

mapfile -t sorted < <(printf '%s\n' "${records[@]}" | sort -t $'\t' -k1,1nr)
n=${#sorted[@]}

print_record() {
    local _started _is_running line
    IFS=$'\t' read -r _started _is_running line <<< "$1"
    printf '%s\n' "$line"
}

if [ "$n" -gt 4 ] && [ "$running" -gt 3 ]; then
    shown_running=0
    for ((i = 0; i < 3; i++)); do
        IFS=$'\t' read -r _started is_running line <<< "${sorted[$i]}"
        [ "$is_running" = "1" ] && shown_running=$((shown_running + 1))
        printf '%s\n' "$line"
    done
    printf '+%d more DeepSeek jobs running\n' $((running - shown_running))
else
    for ((i = 0; i < n && i < 4; i++)); do
        print_record "${sorted[$i]}"
    done
fi
exit 0
