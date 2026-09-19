"""A small but FAITHFUL iptables stand-in for driving llm-isolation.sh.

Faithful in the three ways the script actually depends on:
  * `-I CHAIN N ...` inserts AT position N, so the script's order checks
    (established < allow < deny) mean something. A stub that appended would
    have reported the rules in exactly the wrong order and "proved" the
    opposite of the property under test.
  * `-C CHAIN ...` answers from the same store the inserts wrote to, keyed on
    the rule body, so the read-backs can fail.
  * `-n -L CHAIN --line-numbers` prints `num target prot opt src dst`, the
    format WITHOUT `-v` - which is the whole point of the column question a
    review round got wrong.

State lives in IPT_STATE as one JSON object of chain -> list of rules.
"""
import json
import os
import sys

# SEPARATE STATE PER FAMILY. Wiring ip6tables to the IPv4 store made the
# script's IPv6 cleanup loop delete the IPv4 rules it had just installed -
# a harness bug that looked exactly like a script bug for two rounds of
# debugging. iptables and ip6tables are different tables in reality, and a
# stub that conflates them tests a machine that does not exist.
_V6 = "--v6" in sys.argv
STATE = os.environ["IPT_STATE6"] if _V6 else os.environ["IPT_STATE"]
PRESEED = os.environ.get("IPT_PRESEED6" if _V6 else "IPT_PRESEED", "")


def load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return json.loads(PRESEED) if PRESEED else {}


def save(db):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(db, fh)


def target_of(args):
    if "-j" in args:
        return args[args.index("-j") + 1]
    return "RETURN"


def comment_of(args):
    if "--comment" in args:
        return args[args.index("--comment") + 1]
    return ""


def body(args):
    """The rule minus the chain and any insert position - the identity a -C
    check has to match."""
    return " ".join(a for a in args if a not in ("-I", "-A", "-C"))


def main(argv):
    db = load()
    a = [x for x in argv if x != "--v6"]

    # FAULT INJECTION, so the fence's mid-rebuild failure can be REACHED.
    #
    # The interesting window is between deleting the old rules and inserting
    # the new ones: that is the only moment the lane could be unfenced, and it
    # is unreachable from outside because those inserts are consecutive. Set
    # IPT_FAIL to a substring of the rule and the matching command fails, the
    # way a real iptables would on a kernel module that is not loaded.
    _fail = os.environ.get("IPT_FAIL", "")
    if _fail and _fail in " ".join(a):
        sys.stderr.write("iptables: stub-injected failure\n")
        return 1

    # -n is a display flag; drop it so the verbs line up.
    if "-n" in a:
        a.remove("-n")

    if "-L" in a:
        i = a.index("-L")
        chain = a[i + 1] if len(a) > i + 1 else "INPUT"
        if chain not in db:
            sys.exit(1)
        print("Chain %s (policy ACCEPT)" % chain)
        print("num  target     prot opt source               destination")
        for n, r in enumerate(db[chain], 1):
            print("%-4d %-10s 0    --  %-20s %-20s /* %s */"
                  % (n, r["target"], r.get("src", "0.0.0.0/0"),
                     r.get("dst", "0.0.0.0/0"), r["comment"]))
        return 0

    if "-N" in a:
        db.setdefault(a[a.index("-N") + 1], [])
        save(db)
        return 0

    if "-C" in a:
        chain = a[a.index("-C") + 1]
        want = body(a[a.index("-C") + 2:])
        for r in db.get(chain, []):
            if r["body"] == want:
                return 0
        return 1

    if "-I" in a:
        i = a.index("-I")
        chain = a[i + 1]
        rest = a[i + 2:]
        pos = 1
        if rest and rest[0].isdigit():
            pos = int(rest[0])
            rest = rest[1:]
        src = rest[rest.index("-s") + 1] if "-s" in rest else "0.0.0.0/0"
        dst = rest[rest.index("-d") + 1] if "-d" in rest else "0.0.0.0/0"
        rule = {"target": target_of(rest), "comment": comment_of(rest),
                "body": body(rest), "src": src, "dst": dst}
        db.setdefault(chain, [])
        db[chain].insert(max(pos - 1, 0), rule)
        save(db)
        return 0

    if "-D" in a:
        i = a.index("-D")
        chain = a[i + 1]
        rest = a[i + 2:]
        if rest and rest[0].isdigit() and chain in db:
            idx = int(rest[0]) - 1
            if 0 <= idx < len(db[chain]):
                db[chain].pop(idx)
                save(db)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
