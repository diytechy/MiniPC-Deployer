#!/usr/bin/env bash
# Run Git against ordinary checkouts and Windows-created linked worktrees.
#
# WSL Git treats an absolute `C:/...` gitdir stored in a linked worktree's
# `.git` file as a relative Linux path. Callers then mistake a real checkout for
# an export and can skip source freshness checks or copy ignored secrets. Keep
# the translation here so every image-build consumer asks Git the same way.
#
# DrvFs exposes the CRLF files produced by Windows Git without teaching WSL
# Git the Windows checkout's global autocrlf/filemode policy. Without matching
# that policy, a clean checkout can appear wholly modified and every rebuilt
# image is stamped `+dirty`. Apply the compatibility settings only to WSL drive
# mounts; native Linux worktrees retain their own Git configuration.

repo_git() {
    local requested="$1" pointer_root line windows_git_dir git_dir
    local -a compat=()
    shift

    case "$requested" in
        /mnt/[A-Za-z]/*)
            if command -v wslpath >/dev/null 2>&1; then
                compat=(-c core.autocrlf=true -c core.filemode=false)
            fi
            ;;
    esac

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
                git "${compat[@]}" --git-dir="$git_dir" --work-tree="$pointer_root" -C "$requested" "$@"
                return
                ;;
        esac
    fi

    git "${compat[@]}" -C "$requested" "$@"
}
