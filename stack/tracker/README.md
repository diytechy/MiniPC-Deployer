# `tracker/` — DEPRECATED reference only

These files (`Dockerfile.deprecated`, `entrypoint.sh.deprecated`) are the
**migrated originals** from `life-tracker/deploy/tracker/`. They are kept for
history and are **not** part of any build here.

The canonical NagLight web-container build now lives in the **NagLight** repo
(WI-10.4, decision D1: the container is NagLight's own release artifact). This
stack consumes the resulting image as `naglight:local`:

```sh
docker build -t naglight:local ../NagLight     # from a sibling NagLight checkout
docker compose up -d                            # in ../ (the stack dir)
```

Or uncomment the `build:` fallback in `../docker-compose.yml` to have compose
build the image straight from a sibling `../../NagLight` checkout.
