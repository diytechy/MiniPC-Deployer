#!/usr/bin/env bash
# Run Git against ordinary checkouts and Windows-created linked worktrees.
#
# WSL Git treats an absolute `C:/...` gitdir stored in a linked worktree's
# `.git` file as a relative Linux path. Callers then mistake a real checkout for
# an export and can skip source freshness checks or copy ignored secrets. Keep
# the translation here so every image-build consumer asks Git the same way.

repo_git() {
    local requested="$1" pointer_root line windows_git_dir git_dir
    shift

    pointer_root="$requested"
    while [ "$pointer_root" != / ] && [ ! -e "$pointer_root/.git" ]; do
        pointer_root="$(dirname "$pointer_root")"
    done

    if [ -f "$pointer_root/.git" ]; then
        IFS= read -r line < "$pointer_root/.git" || true
        case "$line" in
            gitdir:\ [A-Za-z]:[\\/]*)
                command -v wslpath >/dev/null 2>&1 || return 128
                windows_git_dir="${line#gitdir: }"
                git_dir="$(wslpath -u "$windows_git_dir")" || return
                git --git-dir="$git_dir" --work-tree="$pointer_root" -C "$requested" "$@"
                return
                ;;
        esac
    fi

    git -C "$requested" "$@"
}
