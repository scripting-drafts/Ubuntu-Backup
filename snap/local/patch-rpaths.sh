#!/bin/bash
# Patch ELF files in a classic snap to use the core base snap's libraries.
set -euo pipefail

prime=${1:?usage: patch-rpaths.sh <prime-dir> <arch-triplet> [base]}
triplet=${2:?usage: patch-rpaths.sh <prime-dir> <arch-triplet> [base]}
base=${3:-core22}

base_root="/snap/${base}/current"
base_lib="${base_root}/lib/${triplet}"
base_usrlib="${base_root}/usr/lib/${triplet}"
snap_lib="${prime}/usr/lib/${triplet}"
snap_usrlib="${prime}/usr/lib"

patched=0
while IFS= read -r -d '' file; do
    # Only process ELF files.
    magic=$(od -An -tx1 -N4 "$file" | tr -d ' \n')
    [ "$magic" = "7f454c46" ] || continue

    interp=$(readelf -l "$file" 2>/dev/null |
        sed -n 's/.*Requesting program interpreter: \(.*\)]/\1/p' | head -n1)
    needed=$(readelf -d "$file" 2>/dev/null | grep -c 'NEEDED' || true)
    if [ -z "$interp" ] && [ "$needed" -eq 0 ]; then
        continue
    fi

    rpath=""
    append_rpath() {
        case ":${rpath}:" in
            *":$1:"*) ;;
            *) rpath="${rpath:+${rpath}:}$1" ;;
        esac
    }

    append_rpath '$ORIGIN'

    rel_lib=$(realpath --relative-to="$(dirname "$file")" "$snap_lib")
    if [ "$rel_lib" = "." ]; then
        append_rpath '$ORIGIN'
    else
        append_rpath "\$ORIGIN/${rel_lib}"
    fi

    rel_usrlib=$(realpath --relative-to="$(dirname "$file")" "$snap_usrlib")
    if [ "$rel_usrlib" = "." ]; then
        append_rpath '$ORIGIN'
    else
        append_rpath "\$ORIGIN/${rel_usrlib}"
    fi

    append_rpath "$base_lib"
    append_rpath "$base_usrlib"
    append_rpath "${base_root}/lib"
    append_rpath "${base_root}/usr/lib"

    patchelf --force-rpath --set-rpath "$rpath" "$file"

    if [ -n "$interp" ]; then
        case "$interp" in
            /lib64/*) base_interp="${base_root}/lib64/${interp#/lib64/}" ;;
            /lib/*) base_interp="${base_root}/lib/${interp#/lib/}" ;;
            *) base_interp="${base_root}${interp}" ;;
        esac
        if [ "$interp" != "$base_interp" ]; then
            patchelf --set-interpreter "$base_interp" "$file"
        fi
    fi

    patched=$((patched + 1))
done < <(find "$prime" -type f -print0)

echo "Patched ${patched} ELF files for ${base} (${triplet})"
