#!/usr/bin/env bash
# Claude Code status line: show ds-bridge progress for this chat's jobs in
# full, and other chats' running jobs as one "+N … in other chats" line.
# Needs only jq and awk. Reads the session id from Claude Code's stdin JSON.
me=$(jq -r '.session_id // ""' 2>/dev/null)

P="$HOME/.local/share/ds-bridge/progress"
TODAY="$HOME/.local/share/ds-bridge/today.json"

fmt_num() {
    awk -v n="$1" 'BEGIN {
        sign = (n < 0 ? "-" : ""); n = (n < 0 ? -n : n)
        if (n >= 10000000) printf "%s%.0fM", sign, n / 1000000
        else if (n >= 1000000) printf "%s%.1fM", sign, n / 1000000
        else if (n >= 10000) printf "%s%.0fk", sign, n / 1000
        else if (n >= 1000) printf "%s%.1fk", sign, n / 1000
        else printf "%s%d", sign, n
    }'
}

idle() {
    [ -f "$TODAY" ] || return 0
    local data day jobs saved
    data=$(jq -r '[(.date // ""), ((.jobs // 0) | floor),
                   ((.saved_tokens // 0) | floor)] | map(tostring) | join("\u001f")' \
        "$TODAY" 2>/dev/null) || return 0
    [ -n "$data" ] || return 0
    IFS=$'\x1f' read -r day jobs saved <<< "$data"
    [ "$day" = "$(date +%F)" ] || return 0
    [ "$jobs" -gt 0 ] 2>/dev/null || return 0
    printf 'DeepSeek idle · %s jobs today · ≈%s tok saved\n' \
        "$jobs" "$(fmt_num "$saved")"
}

if [ ! -d "$P" ]; then
    idle
    exit 0
fi

shopt -s nullglob
files=("$P"/*.json)
if [ ${#files[@]} -eq 0 ]; then
    idle
    exit 0
fi

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
others=0
for f in "${files[@]}"; do
    data=$(jq -r '[(.state // ""), (.job // ""), (.round // 0), (.steps // 0),
                   (.tools // 0), (.out_tokens // 0), ((.started // 0) | floor),
                   ((.updated // 0) | floor), (.last // ""), ((.secs // 0) | floor),
                   (.tok_s // ""), ((.pid // 0) | floor), (.phase // ""),
                   (.live_tok_s // ""), ((.tool_since // 0) | floor),
                   (.args_tool // ""), ((.args_since // 0) | floor),
                   (.ttft_s // ""), (.owner // "")]
                  | map(tostring) | join("\u001f")' "$f" 2>/dev/null) || continue
    [ -n "$data" ] || continue
    IFS=$'\x1f' read -r state job round steps tools out_tokens started updated last secs tok_s pid phase live_tok_s tool_since args_tool args_since ttft_s owner <<< "$data"
    last=${last//$'\t'/ }
    last=${last//$'\n'/ }

    tok_s_seg=""
    case "$tok_s" in
        ''|*[!0-9]*) ;;
        *) tok_s_seg=" · ${tok_s} tok/s" ;;
    esac
    ttft_seg=""
    case "$ttft_s" in
        ''|*[!0-9.]*) ;;
        *) ttft_seg=" · ttft ${ttft_s}s" ;;
    esac

    is_running=0
    case "$state" in
        running)
            age=$((now - updated))
            if [ "$pid" -gt 0 ] 2>/dev/null; then
                alive=0
                kill -0 "$pid" 2>/dev/null && alive=1
            elif [ "$age" -gt 120 ]; then
                alive=0
            else
                alive=1
            fi
            if [ "$alive" = "0" ]; then
                [ "$age" -le 120 ] || continue
                line=$(printf '✗ DeepSeek %s interrupted' "$job")
            else
                live_seg="$tok_s_seg"
                case "$phase" in
                    args)
                        args_name=${args_tool:-tool}
                        if [ "$args_since" -gt 0 ] 2>/dev/null; then
                            elapsed=$((now - args_since))
                            [ "$elapsed" -lt 0 ] && elapsed=0
                            live_seg=" · ✎ ${args_name} ${elapsed}s"
                        else
                            live_seg=" · ✎ ${args_name}"
                        fi
                        ;;
                    tool)
                        if [ "$tool_since" -gt 0 ] 2>/dev/null; then
                            elapsed=$((now - tool_since))
                            [ "$elapsed" -lt 0 ] && elapsed=0
                            live_seg=" · ⏸ ${elapsed}s"
                        else
                            live_seg=" · ⏸"
                        fi
                        ;;
                    writing)
                        case "$live_tok_s" in
                            ''|*[!0-9]*) live_seg="$tok_s_seg" ;;
                            *) live_seg=" · ▶ ${live_tok_s} tok/s" ;;
                        esac
                        ;;
                    waiting)
                        live_seg=" · … working"
                        ;;
                esac
                if [ "$age" -gt 1800 ]; then
                    line=$(printf 'DeepSeek %s stalled?' "$job")
                else
                    spinners=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
                    sp=${spinners[$((now % 10))]}
                    line=$(printf '%s DeepSeek %s r%s · %s · step %s · %s tools · %s tok%s · ▸ %s' \
                        "$sp" "$job" "$round" "$(fmt_secs $((now - started)))" \
                        "$steps" "$tools" "$(fmt_tok "$out_tokens")" "$live_seg" "$last")
                fi
                is_running=1
                running=$((running + 1))
            fi
            ;;
        done)
            [ $((now - updated)) -le 120 ] || continue
            line=$(printf '✓ DeepSeek %s done in %s%s%s' "$job" "$(fmt_secs "$secs")" "$tok_s_seg" "$ttft_seg")
            ;;
        error)
            [ $((now - updated)) -le 120 ] || continue
            if [ "$last" = "interrupted" ]; then
                line=$(printf '✗ DeepSeek %s interrupted' "$job")
            else
                line=$(printf '✗ DeepSeek %s failed in %s' "$job" "$(fmt_secs "$secs")")
            fi
            ;;
        *)
            continue
            ;;
    esac

    # Jobs started from another chat: count them while running, hide otherwise.
    # A job with no owner (started outside Claude Code) shows everywhere.
    if [ -n "$owner" ] && [ -n "$me" ] && [ "$owner" != "$me" ]; then
        if [ "$is_running" = "1" ]; then
            others=$((others + 1))
            running=$((running - 1))
        fi
        continue
    fi

    records+=("$started"$'\t'"$is_running"$'\t'"$line")
done

others_line() {
    [ "$others" -gt 0 ] || return 0
    if [ "$others" -eq 1 ]; then
        printf '+1 DeepSeek job in another chat\n'
    else
        printf '+%d DeepSeek jobs in other chats\n' "$others"
    fi
}

if [ ${#records[@]} -eq 0 ]; then
    if [ "$others" -gt 0 ]; then others_line; else idle; fi
    exit 0
fi

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
others_line
exit 0
