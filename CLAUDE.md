# CLAUDE.md

- Documentation must always be kept up to date. Any change to `docker-compose.yml`, `Dockerfile` or the `redeployer/` code (behavior, env vars, schedule, healthcheck, HTTP API, history, alerts, log output) must update `README.md` in the same change. Run `python -m unittest discover -s tests -t .` before committing.
- Claude's knowledge from memory is not reliable and must be verified before it is stated as fact or written into the compose file or README. Check it against a real source: the Coolify MCP, the local tools and files (`docker`, `--help`, `man`, the repo itself), or the official documentation online (Coolify, Docker Compose, BusyBox, Alpine). When something cannot be verified, say so explicitly instead of presenting it as certain.
