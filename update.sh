#!/bin/bash
# update.sh
#
# Update an existing SongRecPi from its git clone on the Pi:
#   bash update.sh [--songrec] [--system] [--desktop | --lite]
#
#   --songrec   also rebuild songrec from source (slow; needs ~1.5 GB free)
#   --system    also run `apt full-upgrade` first
#
# Pulls the latest code (fast-forward only), then runs `install.sh --update`, which
# redeploys the app, migrates an older install and restarts the app and kiosk.

# Everything is inside main() so bash has read the whole file before `git pull`
# can replace it.
main() {
    set -euo pipefail
    local repo_dir args=() system=0
    repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    for arg in "$@"; do
        case "$arg" in
            --system) system=1 ;;
            --songrec|--desktop|--lite) args+=("$arg") ;;
            -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
            *) echo "Unknown option: $arg" >&2; exit 2 ;;
        esac
    done

    if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "$repo_dir is not a git clone, so it can't be updated in place. Clone it with:" >&2
        echo "  git clone https://github.com/EvanPeacock/SongRecPi.git ~/SongRecPi" >&2
        echo "  cd ~/SongRecPi && bash update.sh" >&2
        exit 1
    fi

    echo "Pulling latest code ($(git -C "$repo_dir" rev-parse --abbrev-ref HEAD))..."
    if [ "${DRY_RUN:-0}" = 1 ]; then
        echo "  + git pull --ff-only"
    else
        git -C "$repo_dir" pull --ff-only
    fi

    if [ "$system" = 1 ]; then
        echo "Free space on /: $(df -Pm / | awk 'NR==2 {print $4}') MB"
        if [ "${DRY_RUN:-0}" = 1 ]; then
            echo "  + sudo apt-get update && sudo apt-get full-upgrade -y && sudo apt-get clean"
        else
            sudo apt-get update
            sudo apt-get full-upgrade -y
            sudo apt-get clean
        fi
    fi

    exec bash "$repo_dir/install.sh" --update "${args[@]}"
}
main "$@"
exit
