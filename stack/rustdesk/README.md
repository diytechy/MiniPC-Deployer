# rustdesk — self-hosted remote desktop

Implements **SN-045**, **SR-045**, **LLR-971**, **LLR-972**, **LLR-973**,
**LLR-976**, **LLR-977**.

Reach the dev PC's desktop **and its sign-in screen** from away, including after
a cold wake when nobody is logged in.

---

## The one requirement that is not a preference

**The Windows client must be installed AS A SERVICE, from an administrator
account.**

A portable or user-session process dies the moment you log out, and goes blind
at a UAC prompt because the secure desktop is not in your session. So a machine
woken by the wake service to a login screen would be unreachable in exactly the
state remote access exists for.

This is the *same* property the inference endpoint needs (IF-021) and the same
one the session-attachment agent needs (IF-025). **Services, not sessions** is
one fact three separate goals depend on — and getting it wrong makes all three
half-work, only after a cold wake, which is the worst time to find out.

## Two PINs, and they are not the same thing

1. **RustDesk's permanent password** — Settings → Security → *Set permanent
   password*. This is what allows an unattended connection with nobody present
   to approve it. High-entropy, and **not your Windows password**: a leak of one
   must not hand over the other.
2. **Your Windows Hello PIN** — typed into the credential provider once RustDesk
   is already showing you the login screen. TPM-bound to the device.

Verify the second **against a genuinely logged-out machine, not a locked one.**
They behave differently, and this is the step most likely to surprise you.

## Why the fence is in INPUT and not DOCKER-USER

These services run with `network_mode: host`, so their sockets are **host**
sockets. Traffic to them is delivered locally through `INPUT` and never enters
`DOCKER-USER`, which is consulted for traffic *routed* to a published container
port.

A `DOCKER-USER` rule here would be present, would read correctly to a reviewer,
and **would filter nothing**. An earlier draft of this repo's own requirement
rows specified exactly that; it was caught in cross-review before anything
shipped. `game-isolation.sh` next door reaches the same conclusion for the same
reason — while the fail2ban jail correctly reaches the opposite one, because it
*is* guarding published ports.

Both address families are programmed. A v4-only ruleset leaves the same listener
reachable over IPv6, and the tunnel makes a routable v6 address more likely
rather than less.

## What starts these containers

Only `homehub-rustdesk.service`. Both containers declare `restart: "no"`, so the
container runtime never starts them independently, and the unit `Requires=` the
isolation unit — so a fence failure stops the listeners *existing* rather than
merely losing a race to them.

A deliberate manual `docker compose` start by the Owner on their own machine is
not defended against; per the standing threat model this repo does not carry a
mechanism for that. The property that matters is that nothing starts them
**without someone deciding to**.

## The key pair

`hbbs` and `hbbr` share **one** volume so they read one key pair. Record the
public key at first start — every client must carry it, and a server reachable
without it admits any caller who can reach it.

The volume is **not reproducible from this repo** and regenerating the key
invalidates every installed client, so it joins the **local** backup set. Never
the offsite set.

## Reachability

**No router forward, ever.** The remote path is the mesh tunnel (SR-042), and
that is precisely what makes the no-forward rule affordable rather than a
limitation.

Know the trade you accepted: **RustDesk does not work until the tunnel is up**,
so if the tunnel is what is broken, this is not your way back in. Keep the
existing hub SSH path in mind for that case.

## Ports

| Process | TCP | UDP |
|---|---|---|
| `hbbs` (ID/rendezvous) | 21115, 21116, 21118 | 21116 |
| `hbbr` (relay) | 21117, 21119 | — |

## Bring-up order

1. Enable the profile and start `homehub-rustdesk.service`.
2. Record the public key.
3. Install the Windows client **as a service, from an admin account**. Point ID
   and relay at the hub, set the key, set the permanent password.
4. Test from the LAN: connect while logged in; then lock; then **log out fully**
   and confirm you reach the sign-in screen and can enter the PIN.
5. Retest the whole path over the tunnel from cellular.
6. Only then wire it to the wake path: cold box → magic packet → sign-in screen
   → PIN → desktop, end to end.


## A listener that dies while docker says "Up" (2026-09-28)

`hbbs` runs a periodic self-test against its own `0.0.0.0:21116` and **exits on
a single failed test**. On 2026-09-28 21:55 local the hub hit a memory-pressure
episode (journald "Under memory pressure", containerd/dockerd "context deadline
exceeded"); `hbbs` logged `Failed to run hbbs test with 0.0.0.0:21116: Timeout
of test_hbbs` and exited. Docker **lost the exit event**: `docker ps` kept
saying "Up 4 weeks", `docker top hbbs` listed no process, and nothing listened
on tcp 21115/21116 or udp 21116 for three weeks. `hbbr` kept running. Because
the restart policy is deliberately `no` (see "What starts these containers"),
nothing brought `hbbs` back. It was recreated by hand on 2026-10-18.

`homehub-rustdesk-check.timer` (every 5 minutes) now runs `rustdesk-check.sh`,
which asks the **listening table** (`ss -H -ltnu`) rather than docker, because
docker state is exactly what lied. Required: `hbbs` tcp 21115, tcp 21116, udp
21116; `hbbr` tcp 21117 (the 21118/21119 websocket pair is not required). It
does nothing unless `RUSTDESK_ENABLED=true` and `homehub-rustdesk.service` is
active. On a missing listener it confirms `homehub-rustdesk-isolation.service`
is active (if not: alert, start nothing), force-recreates only the affected
service (`docker compose --profile rustdesk up -d --no-build --force-recreate
--no-deps`; the keys live in the named volume, so this is safe), re-checks, and
alerts through the hub's own ntfy: recovered, or still down (high priority and a
failed unit, so `systemctl --failed` shows it).

Run it by hand: `sudo /opt/homehub/stack/rustdesk/rustdesk-check.sh`
(`journalctl -u homehub-rustdesk-check` for the timer's runs).
