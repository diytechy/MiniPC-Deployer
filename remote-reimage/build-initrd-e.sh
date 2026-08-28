set -e
B="${OUTDIR:-/tmp/initrd-e}"   # override with OUTDIR=; needs ~160 MB
W="$B/ebuild"
TREE="${TREE:-/mnt/z/hub-isotree}"   # the extracted ISO tree served over SMB
rm -rf "$W"; mkdir -p "$W/root/conf"; cd "$W"

cat > root/conf/param.conf <<'EOF'
# INJECTED FOR REMOTE_MANAGEMENT.md OPTION E (kexec netboot over CIFS).
#
# WHY THIS FILE EXISTS. casper parses `nfsroot=` and `netboot=` off the kernel
# command line but has NO `nfsopts=` case - measured 2026-08-28 against the
# shipped /scripts/casper. So do_cifsmount always takes its hardcoded fallback,
#     CIFSOPTS="-ouser=root,password="
# and a Windows share answers STATUS_LOGON_FAILURE / mount error(13). There is
# no supported way to hand casper CIFS credentials. That single fact is why every
# Option E attempt failed identically on real hardware and in a VM.
#
# HOW THIS GETS RUN. scripts/functions run_scripts() SOURCES the hook directory's
# ORDER file, and ORDER sources /conf/param.conf after each hook:
#     [ -e /conf/param.conf ] && . /conf/param.conf
# That is casper's own channel for a hook to set variables in its own scope, and
# it runs inside mountroot() BEFORE the `if [ ! -z "${NETBOOT}" ]` netboot block.
#
# NO SECRET IS BAKED IN. The options ride the kernel command line as
# `cifsopts=-ouser=...,password=...` - where the credential already had to live -
# and this only copies it into the variable casper actually reads.
for _x in $(cat /proc/cmdline); do
    case $_x in
        cifsopts=*) export NFSOPTS="${_x#cifsopts=}" ;;
    esac
done
unset _x
EOF

( cd root && find . | cpio --quiet -o -H newc ) > extra.cpio
SZ=$(stat -c %s extra.cpio); PAD=$(( (4 - SZ % 4) % 4 ))
cp extra.cpio head.img
[ "$PAD" -gt 0 ] && head -c "$PAD" /dev/zero >> head.img
cat head.img "$TREE/casper/initrd" > initrd-e
echo "extra=$SZ pad=$PAD result=$(stat -c %s initrd-e)"

echo "--- verify (prepend = the supported early-cpio layout) ---"
rm -rf verify; mkdir verify; cd verify
# VERIFICATION MUST FAIL THE BUILD. The first attempt at this appended the extra
# segment AFTER the compressed main archive; unmkinitramfs could not read it back
# and the check printed "warned" and carried on. A build that cannot be unpacked
# is a box that will not boot, so anything short of all three assertions is fatal.
unmkinitramfs ../initrd-e . >/dev/null 2>&1     || { echo "  FAIL: unmkinitramfs cannot read the result back"; exit 1; }
echo "  unmkinitramfs OK"
P=$(find . -name param.conf | head -1)
C=$(find . -name casper -path '*scripts*' | head -1)
U=$(find . -name uuid.conf | head -1)
[ -n "$P" ] || { echo "  FAIL: param.conf is not in the rebuilt initrd"; exit 1; }
[ -n "$C" ] || { echo "  FAIL: scripts/casper missing - the base image was damaged"; exit 1; }
[ -n "$U" ] || { echo "  FAIL: conf/uuid.conf missing - matches_uuid would reject the medium"; exit 1; }
grep -q 'cifsopts=' "$P" || { echo "  FAIL: param.conf does not read cifsopts="; exit 1; }
echo "  param.conf:   $P"
echo "  casper:       $C"
echo "  medium uuid:  $(cat "$U")"
echo "BUILD OK - copy ebuild/initrd-e to <tree>/casper/initrd-e"
