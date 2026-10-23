# web/ - OtterTube's served build (SN-051)

Caddy serves this directory read-only at `https://<OTTERTUBE_HOST>/ottertube/`
(docker-compose.yml mounts `./ottertube/web`). It ships empty: the coordinator
copies OtterTube's `dist/web` from the recorded commit INTO it with
`rsync -a --delete` (never by replacing the directory, which would detach the
bind mount). `build-info.json` at its root names the commit. See
`../README.md` "Deploying the web build and the indexer".
