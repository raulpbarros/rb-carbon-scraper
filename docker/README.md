# Running the scraper in Docker

The long half of this project is headless: `verra sync` is ~9,000 requests and
about 2.5 hours for Verra, ~7,300 and about 2 hours for Gold Standard. None of
it needs a window. Putting that in a container gets it off the desktop, keeps
the ~1 GB response cache and the ~215 MB database out of the checkout, and
makes a refresh one command that is safe to interrupt.

**It does not replace `build.ps1`.** PyInstaller needs a Windows host, so the
EXE, the portable ZIP and the installer are still built on Windows. Docker
covers the scraping, not the delivery.

## Two images

| Target | What it is | Size driver |
|---|---|---|
| `runtime` | the scraper — `verra` on `python:3.11-slim` | httpx, openpyxl |
| `discovery` | the diagnostic — adds Playwright and one Chromium | ~400 MB of browser |

`discover` is not the data path; every registry is reached over plain httpx.
The scraping image has no browser in it, exactly as the frozen build has none.

## The commands

Everything is a job: it runs, it exits. Nothing is a daemon and nothing is
scheduled — on a laptop, a scheduler inside a container stops when Docker
Desktop stops, which is whenever the lid closes, and misses runs with no
signal at all.

```powershell
cd docker

docker compose build sync                     # first time, ~2 min

docker compose run --rm sync                  # refresh every registry
docker compose run --rm sync update -r gs     # ... or one of them
docker compose run --rm sync update -r verra --limit 25   # smoke test

docker compose run --rm status                # row counts, last runs, failures
docker compose run --rm export                # write the next out/carbon-projects_vN.xlsx
docker compose run --rm publish               # copy the database into data/
docker compose run --rm discover              # re-capture an S&P contract
```

`Ctrl-C` is a clean stop. Every write is an idempotent upsert, so a cancelled
sync is repaired by running it again — it does not restart from zero either,
because the responses it already fetched are in the cache on the volume.

`sync` runs `verra update`: sync → Verra's exact-totals pass → derive, and
**no export**. That is the same line the window's `Update registry data`
button draws, through the same `pipeline.update_all()`. A refresh that wrote a
spreadsheet would burn a version number every night and the business would
receive `_v9` without anyone having decided to send anything.

## Where the data lives, and why it is not a bind mount

`CARBON_HOME=/data`, a **named Docker volume** (`carbon-registry_carbon-data`).
The database, the response cache, the logs and the user-editable copies of
`assets/fields-asked.txt` and `config/derivation/*.yaml` all live there.

It is not a bind mount from the checkout, and that is not a preference:

> `db.py` opens SQLite with `PRAGMA journal_mode=WAL`. WAL needs POSIX
> advisory locks and a shared-memory `-shm` mapping alongside the database
> file. Docker Desktop's Windows bind mounts (virtiofs / gRPC-FUSE) provide
> neither reliably. The failure is not a clean error — it is a database that
> still opens.

Named volumes live on ext4 inside the Linux VM, where WAL behaves normally.

Three services do bind-mount, because none of them touches SQLite over the
mount:

- `export` → `../out`, a plain sequential `.xlsx` write. It shares `out/` with
  the Windows app on purpose, so there is one version history rather than two,
  and `_vN+1` still applies.
- `publish` → `../data`, written with `VACUUM INTO` (see below).
- `discover` → the whole checkout, because its captures under `docs/` and
  `tests/fixtures/` belong in git.

To edit a derivation rule for the container, edit the copy on the volume — the
same path an installed user takes:

```powershell
docker compose run --rm --entrypoint sh sync -c "cat /data/config/derivation/biome.yaml"
```

### Getting the database back to Windows

```powershell
docker compose run --rm publish                    # -> data/verra.db
docker compose run --rm publish /publish/verra.db --force
```

Not `docker cp`, and not a file copy. A WAL database's committed state is
spread across `verra.db`, `-wal` and `-shm`; copying the first file alone
produces something that opens without complaint and is missing every
transaction still in the WAL. `docker/publish-db.py` uses `VACUUM INTO`, which
writes one consistent, checkpointed, compacted file from a read snapshot — so
it is also safe to run while a sync is in progress.

It refuses to replace an existing `data/verra.db` without `--force`. That file
is what the Windows app and `build.ps1` read, and a two-hour scrape is not
recoverable from an accidental overwrite.

This is not `verra slim-db`: that produces the ~21 MB delivery artifact the
installer carries, with the bulk ledgers dropped. `publish` hands over the full
working database.

## Things that would waste an afternoon

- **The install must stay editable.** `settings.RESOURCE_ROOT` is
  `Path(__file__).resolve().parents[2]`, so the package has to keep sitting
  next to `assets/`, `config/` and `docs/`. Those are not package data —
  setuptools collects `src/` only — so a plain `pip install .` puts the
  package in site-packages where none of them exist. Nothing raises: the
  requested-field list comes back empty. `tests/test_docker.py` pins it.
- **Chromium is installed with `playwright install`, not inherited from
  `mcr.microsoft.com/playwright/python`.** `playwright install` fetches the
  browser build matching the wheel pip just resolved, so the two cannot drift.
  A pinned image tag that a newer wheel outruns fails at run time with
  "Executable doesn't exist", after the download.
- **The IP is the same one.** The container NATs out through the host, so
  Cloudflare in front of Gold Standard and Cercarbono's `Origin` check see
  exactly what they see today. Moving this to a cloud VM would change that,
  and is the one variable worth not changing casually.
- **Politeness is unchanged.** Same ~1 req/s, same backoff, same cache. Do not
  run several registries in parallel containers to "make it faster".

## What Docker does not solve

SmartScreen, code signing and antivirus — the Phase 4b blockers — are all
about a Windows executable arriving on someone else's machine. Nothing here
touches them.
