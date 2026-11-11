# How the library stays current

BEQ Encoder tracks every `.mkv` file under `MEDIA_ROOTS` without help from you. It scans, it watches, and it assesses.

## Scans

The encoder scans every media root at start-up and on a schedule, daily by default.
Scans are incremental. The encoder does not probe a file again when its size and its modification time are unchanged.
`SCAN_ON_START` and `SCAN_INTERVAL_HOURS` control the scans.

## Watching for changes

The encoder watches the media roots for new, changed, and deleted `.mkv` files through inotify.
The encoder probes a new file only after the size of the file stops changing for `WATCH_SETTLE_SECONDS`.
A file that is still being copied is therefore never read half-written.

inotify sees only the writes made on the same machine.
If another machine writes to the library over a network share, set `WATCH=poll` or rely on the scheduled scan.

A large library needs one inotify watch per folder. Check the limit with `sysctl fs.inotify.max_user_watches`.

On Docker Desktop, inotify on a bind mount can miss files written in the first minutes after the container starts.
The start-up scan and the scheduled scan find those files.

## Assessment

The encoder assesses every file, and assesses it again when the catalogue, the designer, or the file changes.
The library page groups the results into views.

### BEQ candidates

A file is a candidate when it has no BEQ track and one of these is true:

- The catalogue has an entry for its best track.
- The bass of its best track shows a clear roll-off from mastering.

The bass screen finds the roll-off.
It reads ten 30-second excerpts spread through the film, at idle priority, and only while no job runs.
It judges the peak spectrum of the best track on three measures:

- The frequency where the spectrum falls 6 dB below its 40 to 80 Hz level.
- The level at 15 Hz.
- The slope of the roll-off.

The bass screen is a filter for your attention, and it designs nothing. The designer analyses the whole film when you run it.
To turn the bass screen off, set `ASSESS_AUDIO=false`.

### Stale BEQ tracks

The encoder judges a BEQ track from the tags of the track. A BEQ track is stale when any of these is true:

- Its catalogue entry was updated or withdrawn.
- The designer made it, and the catalogue now has an entry for the track.
- The designer now produces a different design version.
- An encoder revision with better output has replaced the revision that made it.
- A better output is now possible, such as TrueHD where only a test FLAC exists.
