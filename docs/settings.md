# Settings

BEQ Encoder reads every setting from an environment variable.

## Values in `.env`

`compose.yaml` reads these values from `.env`. Copy `.env.example` to start.

| Variable | Default | Meaning |
|---|---|---|
| `PUID`, `PGID` | `1000` | The user and the group that both containers run as. Use the owner of the media files. |
| `DEE_PATH` | none, required | The host folder that holds the Linux DEE 6.7 tools and `dee_dthd_encoder`. Mounted read-only at `/opt/dee` in the container. |
| `WORK_DIR` | none, required | The host folder for scratch files, mounted at `/work` in both containers. |
| `MEDIA_DIR` | none, required | The host folder of the film library, mounted read-write at `/media/movies`. |
| `BEQFORGE_VERSION` | `0.3.3` | The image tag of `beqforge-designer`. |
| `BEQFORGE_WORKERS` | `1` | The number of designer workers. |
| `TZ` | `UTC` | The time zone of the encoder. |

## Encoder variables

Set these variables in the `environment` block of the `beq-encoder` service in `compose.yaml`.

### Encoding

| Variable | Default | Meaning |
|---|---|---|
| `DEE_PATH` | `/opt/dee` | The folder that holds `dee_dthd_encoder`, as seen in the container. `compose.yaml` mounts the DEE folder there. If you change this value, change the mount target too. |
| `DEE_SHIM` | `/usr/local/bin/dee_shim` | The path of dee_shim. |
| `TARGET_PEAK_DBFS` | `-1.0` | The peak of the filtered track, which the automatic gain meets on every route. |
| `MAX_CONCURRENT_JOBS` | `1` | The number of jobs that run at once. |
| `KEEP_WORK` | `false` | Keeps the DAMF, WAV, and MLP intermediates after a job. |
| `ALLOW_TEST_FORMATS` | `false` | Offers FLAC output for channel-based sources. For development only. |

### The BEQ track

| Variable | Default | Meaning |
|---|---|---|
| `TRACK_NAME_TEMPLATE` | `BEQ {codec} ({source})` | The name of the BEQ track. Also accepts `{orig}` and `{author}`. |
| `BEQ_TRACK_DEFAULT` | `false` | Makes the BEQ track the default track. |
| `BEQ_CHARTS` | `true` | Makes the charts and the report for every BEQ track. |

### Catalogue and designer

| Variable | Default | Meaning |
|---|---|---|
| `CATALOGUE_URL` | the `database.json` of beqcatalogue | The source of the catalogue. |
| `CATALOGUE_MAX_AGE_DAYS` | `1` | The age at which the scan schedule downloads the catalogue again. |
| `DESIGNER_URL` | `http://beqforge-designer:8420` | The address of the beqforge designer. |
| `DESIGNER_TIMEOUT` | `1800` | The time limit for one design, in seconds. |
| `BM_CROSSOVER_HZ` | `80` | The crossover of the bass-management model sent to the designer. `0` sends no model. |
| `BM_LPF_POSITION` | `Before` | The low-pass position of the bass-management model. |
| `BM_HEADROOM` | `WCS` | The headroom rule of the bass-management model. |
| `ANALYSIS_CACHE_GB` | `20` | The size limit of the 1 kHz analyses under `/work/analysis`. |

### Library

| Variable | Default | Meaning |
|---|---|---|
| `MEDIA_ROOTS` | `/media` | The roots that the encoder scans for `.mkv` files, separated by colons. |
| `SCAN_ON_START` | `true` | Runs an incremental scan when the container starts. |
| `SCAN_INTERVAL_HOURS` | `24` | The hours between scheduled scans. `0` turns scheduled scans off. |
| `SCAN_WORKERS` | `4` | The number of files probed at once during a scan. |
| `WATCH` | `inotify` | How the encoder watches the roots: `inotify`, `poll`, or `off`. |
| `WATCH_POLL_SECONDS` | `60` | The interval between polls when `WATCH=poll`. |
| `WATCH_SETTLE_SECONDS` | `60` | How long the size of a new file must stay unchanged before the encoder probes the file. |
| `ASSESS_AUDIO` | `true` | Runs the background bass screen on films without a BEQ track. |

### Service

| Variable | Default | Meaning |
|---|---|---|
| `HOST` | `0.0.0.0` | The address that the web UI listens on. |
| `PORT` | `8080` | The port that the web UI listens on. |
| `LOG_LEVEL` | `info` | The log level. |
