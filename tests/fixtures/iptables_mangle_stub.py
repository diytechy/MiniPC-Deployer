"""A FAITHFUL-ENOUGH iptables stand-in for one table and its built-in chains.

Built for tailnet-keep-source.sh and setup-tailscale.sh --check-only. What it
must get right, because the script depends on it:
  * `-S CHAIN` prints `-P CHAIN ACCEPT` and then one `-A CHAIN <spec>` line per
    rule, in order - the format the script parses (iptables 1.8.10).
  * `-C`, `-A` and `-D` act on the SAME store, keyed on the exact argument
    list, so a read-back can fail and a delete by spec removes one rule only.
  * `-w N` and `-t TABLE` are accepted wherever they appear.

State: IPT_STATE, a JSON object {"<table>/<chain>": [[arg, ...], ...]}.
Calls are appended to IPT_LOG, one JSON list per line.
Faults: IPT_FAIL_VERB=-S makes every listing fail (an unreadable chain);
IPT_IGNORE_APPEND=1 makes -A "succeed" without adding anything.
"""
import json
import os
import sys


def main(argv):
    with open(os.environ["IPT_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")
    a = list(argv)
    table = "filter"
    if "-w" in a:
        i = a.index("-w")
        del a[i:i + 2]
    if "-t" in a:
        i = a.index("-t")
        table = a[i + 1]
        del a[i:i + 2]
    if not a or a[0] not in ("-S", "-C", "-A", "-D"):
        sys.stderr.write("iptables stub: unhandled %r\n" % (argv,))
        return 97
    verb, chain, spec = a[0], a[1], a[2:]
    if os.environ.get("IPT_FAIL_VERB") == verb:
        sys.stderr.write("iptables: stub-injected failure\n")
        return 4

    with open(os.environ["IPT_STATE"], encoding="utf-8") as fh:
        db = json.load(fh)
    rules = db.setdefault("%s/%s" % (table, chain), [])

    if verb == "-S":
        print("-P %s ACCEPT" % chain)
        for r in rules:
            # iptables quotes an argument that holds a space (a comment).
            print("-A %s %s" % (chain, " ".join('"%s"' % x if " " in x else x for x in r)))
        return 0
    if verb == "-C":
        return 0 if spec in rules else 1
    if verb == "-A":
        if os.environ.get("IPT_IGNORE_APPEND") != "1":
            rules.append(spec)
    elif verb == "-D":
        if spec not in rules:
            sys.stderr.write("iptables: Bad rule (does a matching rule exist in that chain?).\n")
            return 1
        rules.remove(spec)
    with open(os.environ["IPT_STATE"], "w", encoding="utf-8") as fh:
        json.dump(db, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
