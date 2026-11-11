# How to build, release, and update BEQ Encoder

## Run the tests

The tests need `ffmpeg` and mkvtoolnix, and no Dolby software.

```sh
cd app && pip install -e '.[test]' && pytest
```

To run the tests in the image, which does not include them, mount `app`:

```sh
docker run --rm -v "$PWD/app:/src/app:ro" --entrypoint sh ghcr.io/ryanmelena/beq-encoder:latest -c \
	'pip install -q pytest httpx && cp -r /src/app /tmp/app && cd /tmp/app && python3 -m pytest -q'
```

The tests replace `truehdd`, `dee_shim`, `dee_dthd_encoder`, `cavern-dump`, and the beqforge designer with fakes.
The fake `dee_shim` and the fake `dee_dthd_encoder` reject a wrong command and code object positions as DEE does.
A green test run therefore says nothing about a new build of the real tools. See [Re-check a new DEE, dee_shim, or truehdd](#re-check-a-new-dee-dee_shim-or-truehdd).

CI runs the same tests in `python:3.13-slim-trixie`, with the `ffmpeg` and the mkvtoolnix that the image ships.

## Build the image yourself

1. In `compose.yaml`, replace the `image:` line of `beq-encoder` with `build: .`.
2. Run `docker compose build`.

The Dockerfile takes these build arguments:

| Argument | Meaning |
|---|---|
| `BASE_IMAGE` | The base image. Any Debian trixie image works, such as one from a registry mirror. |
| `DEE_SHIM_VERSION` | The dee_shim release. |
| `DEE_SHIM_COMMIT` | The commit of that release. The build fails if the commit is not the tag of `DEE_SHIM_VERSION`. |
| `DEE_SHIM_RUST` | The Rust toolchain that the dee_shim release is built with. |
| `TRUEHDD_VERSION` | The truehdd release. |
| `CAVERN_COMMIT` | The Cavern commit. An empty value builds the image without Cavern. |

The build applies every patch in `docker/dee-shim/` to dee_shim.
`dee_shim -V` then reports `<version>+g<commit>-dirty`, where "dirty" marks the patch.

## Release a version

The app version goes into the tags of every BEQ track, so a release tag must match the version.

1. Set the version in `app/beq_encoder/__init__.py` and in `app/pyproject.toml`.
2. If the output of the encoder changed, raise `ENCODER_REVISION` in `app/beq_encoder/assess.py`. The library uses the revision to mark older BEQ tracks as stale.
3. Commit, then push a matching tag: `git tag v1.2.3 && git push origin v1.2.3`.

`.github/workflows/publish.yml` runs the tests, builds the image for linux/amd64, and pushes it to `ghcr.io/ryanmelena/beq-encoder`.
The workflow refuses a tag that does not match both version files.

| Trigger | Image tags |
|---|---|
| A push to the default branch | `latest`, the branch name, `<branch>-<short sha>` |
| A tag `v1.2.3` | `latest`, `1.2.3`, `1.2`, `1` |

## Keep the dependencies current

Dependabot opens pull requests for the GitHub Actions and the Python dependencies.

Dependabot cannot see the truehdd and dee_shim versions in the Dockerfile.
`.github/workflows/tooling-updates.yml` checks both projects every Monday and opens a pull request for a newer release.
The pull request does three things:

- It changes the build arguments in the Dockerfile.
- It reports whether each patch in `docker/dee-shim/` still applies.
- It lists the checks that you must run by hand before you merge.

The workflow needs one repository setting.
In **Settings**, open **Actions**, then **General**, and select **Allow GitHub Actions to create and approve pull requests**.

To check for releases now, run the **Tooling updates** workflow from the **Actions** tab.

No workflow tracks dthd-mac2linux. `tools/dee-build/Dockerfile` holds its version in `DTHD_MAC2LINUX_VERSION`, `DTHD_MAC2LINUX_COMMIT`, and `RUST_VERSION`.
To move to a new release, change those three values.

## Pass options when you build the Linux encoder

During setup, the container in `tools/dee-build` builds the Linux `dee_dthd_encoder` from the macOS one. The container is not part of the service.
The container passes every argument after the image name to `dthd-mac2linux build`.

| Argument | Use it when |
|---|---|
| `--verify` | You want two isolated builds compared with each other. The run takes twice as long. |
| `--allow-toolchain-drift` | The build stops because an Ubuntu update changed the compiler versions that dthd-mac2linux pins. |
| `--allow-unpinned-payload` | Your macOS `dee_dthd_encoder` is not from DEE 6.7.0+8e353f99. The result needs the re-check in the next section. |

The container also writes `BUILDINFO.json` and `SHA256SUMS` beside the encoder. `BUILDINFO.json` records the hash of the macOS file that you supplied.

## Re-check a new DEE, dee_shim, or truehdd

Do these steps before you merge a tooling pull request or change your DEE folder.

1. Run the start-up probe. dee_shim refuses a DEE build that it cannot patch.

	```sh
	dee_shim dthd_encoder --binary $DEE_PATH/dee_dthd_encoder --as-is on --oamd-as-is on --drc none -- --help
	```

2. Encode a TrueHD Atmos excerpt and a 5.1 WAV through the UI.
3. Read the checks in the job log, on the lines `TrueHD Atmos check: {...}` and `Fidelity check (TrueHD): {...}`. Confirm these values:
	- Every `element_err_lsb` is at most 0.5 for Atmos and at most 1.5 for a channel-based track.
	- `offset_frames` is 0.
	- The position check passes.
4. For a new dee_shim, find out whether `docker/dee-shim/quiet-weight.patch` is still needed. [dee-shim issue 4](https://github.com/truehdd/dee-shim/issues/4) carries `quiet_onset_repro.py`, which runs 42 quiet-object cases and prints one PASS or FAIL line for each case.

[TrueHD encoding through dee_shim](truehd-encoding.md) explains what the checks measure.
