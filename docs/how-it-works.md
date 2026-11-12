# How BEQ Encoder makes a BEQ track

This page explains what happens between **Run BEQ** and the replaced MKV, and why each step exists.
The encode itself has its own page: [TrueHD encoding through dee_shim](truehd-encoding.md).

## A job has three stages

1. **Prepare.** The encoder probes the file with `mkvmerge -J`, `ffprobe`, and MediaInfo. It picks the best track, applies the support rules, and looks for a filter.
2. **Review.** The job waits for you. Pick a filter and click **Encode with this filter**, or click **Don't add BEQ** to leave the film as it is.
3. **Encode.** The encoder filters the track, encodes it, checks it, and remuxes the file.

If any step fails, the source file stays untouched.

## Where the filter comes from

The encoder asks the BEQCatalogue first. An entry counts only when it matches both the film and the type of the track.

- If the entry has filters, the job waits for you to review them.
- If the entry says "no filters", the run ends as **No BEQ**.
- If the catalogue has no entry, the encoder asks the beqforge designer.

The designer can also decline, and the run then ends as **No BEQ**.
No BEQ is advice, and you can overrule it.
**Choose a filter anyway** opens the review page with the catalogue entries for the other tracks of the film and a catalogue search.
The encoder never preselects an entry for a different track or film.

## How the encoder picks the best track

The encoder ranks tracks by format first: lossless with objects, lossless channel-based, lossy with objects, then lossy.
Channel count breaks a tie, then the original-language flag, then the default flag.
The encoder skips commentary tracks, descriptive-audio tracks, forced tracks, and existing BEQ tracks.
You can pick another track on the page of the film.

## The encode, step by step

A TrueHD Atmos source takes this route:

1. `mkvextract` copies the bitstream out of the MKV, byte for byte.
2. `truehdd` decodes the bitstream to a Dolby Atmos master file set (DAMF).
3. The encoder applies the BEQ to every bed channel and every object channel, and writes a second DAMF. The encoder keeps the first DAMF, unfiltered, for the check.
4. `dee_shim` runs `dee_dthd_encoder`, which writes `beq.mlp`.
5. `truehdd` decodes `beq.mlp`. The check compares every sample of every element with the BEQ of the kept decode, and every object position frame by frame.

Every other source takes this route:

1. `ffmpeg` decodes every sample, in order, as float64 at the source rate. The decode does no resampling and no timestamp correction.
2. The encoder applies the BEQ and writes a 24-bit WAV.
3. `dee_shim` runs `dee_dthd_encoder`, which writes `beq.mlp`.
4. The check decodes `beq.mlp` and compares every sample of every channel with the BEQ of the source. The two must agree within 1.5 LSB, which is the 24-bit dither, with no sample missing or added.

A DD+ Atmos source uses the Atmos route, with `cavern-dump` in place of `truehdd` for the first decode.
See [DD+ Atmos through Cavern](dd-plus-atmos.md).

Every job then finishes the same way:

1. The encoder measures the original track and the BEQ track at 1 kHz and draws the [charts](output.md#charts).
2. `mkvmerge` writes a temporary MKV next to the source. The BEQ track follows the source track, is not the default track, and carries tags that name its filter.
3. The encoder verifies the track count, the track names, and the duration.
4. The encoder compares A/V sync and channel order with the original, and remuxes once if the BEQ track is more than 1 ms off.
5. `mkvpropedit` embeds the charts and the report, and the encoder checks the tracks again.
6. The encoder copies the permissions and the owner, then replaces the source in one atomic rename.

## One filter and one gain for every channel

The filter is a single cascade of RBJ biquads with the same coefficients as the catalogue.
The encoder applies the filter to every channel: beds, LFE, and objects.
The filters are linear, so the result equals a filter on the bass-managed sub feed.

One gain, the same for every channel, brings the filtered peak to `TARGET_PEAK_DBFS`.
A gain that clips fails the job, because a clipped sample cannot equal the filtered source.

The encoder dithers a channel-based track to 24 bits with TPDF dither.
The encoder rounds an Atmos master to 24 bits without dither, an error of at most 0.5 LSB.
The reason is DEE: under `--as-is`, DEE codes a rounded master bit for bit, but codes a dithered master 1 to 4 LSB off.

## Timing matches the source track

The BEQ track starts at the same point on the timeline of the film as the source track.
The encoder reads that point from the first decoded frame of the source.
Whole milliseconds go into an `mkvmerge --sync` offset.
The rest, less than a millisecond, becomes leading digital silence.
The encoder measures any silence that DEE adds in front and compensates for it in the mux.

A decoder sometimes produces nothing for part of the timeline.
TrueHD frames before a restart point do that, and so does a gap between joined segments.
The encoder inserts the same length of digital silence, as a player does, and reports the gap on the job page.
The Atmos routes cannot fill such a gap, because the object metadata would have to move with the audio. The job fails instead.

The encoder never trims, resamples, or time-stretches audio.
If the decoded frames do not map onto the timeline, the job fails.
After the mux, the A/V check decodes both tracks at the 30 % point of the film, by timestamp, as a player does.

## Level, DRC, and channel order

**Dialogue level.** The encoder passes the dialogue level of the source to DEE with `--loudness-management skip:dialnorm=N`.
The level comes from `truehdd info` for TrueHD (run on the first 4 MB of the stream, which lists the same presentations as the whole of it) and from MediaInfo for AC-3 and DD+. DTS, PCM, and FLAC carry none, so N is -31.
Without this switch, DEE measures the loudness and writes its own value.
A disc marked -31 can come out as -22, and a decoder that applies dialnorm then plays the BEQ track 9 dB quieter than the original.

**DRC.** DRC is off by default on every presentation, as on the source.
The AC-3 and E-AC-3 decoders in `ffmpeg` apply the dynamic range compression of the stream unless told otherwise.
Every decode therefore uses `-drc_scale 0`, which gives the full dynamic range.

**7.1 channel order.** DEE reads the channels of a WAV by position in Dolby order: L R C LFE Ls Rs Lrs Rrs.
DEE ignores the channel mask, and the 7.1 order of the mask puts the back pair first.
The encoder writes the WAV in Dolby order, so the side surrounds and the rear surrounds never swap.
DeeZy uses the same remap: `pan=7.1|…|c4=c6|c5=c7|c6=c4|c7=c5`.

## What the designer receives

`beqforge-designer` implements the [designer contract of beqdesigner](https://github.com/3ll3d00d/beqdesigner/blob/master/design/designer-interface.md) over HTTP.

The encoder decodes the selected track once at 1 kHz in double precision, with `aformat=sample_fmts=dbl,aresample=1000`.
It writes a float64 WAV under `/work/analysis/<key>/`.
The columns are the channels in `ffmpeg` layout order (`ffmpeg_layout_v1`), plus a mono mix.
The mono mix is the mains at -20.2 dB plus the LFE at -10.2 dB.

The encoder sends each array by reference, as a path and a SHA-256, under contract 1.2.
Both containers mount the same `/work`, so the designer reads the file directly.
The request also carries `coverage: complete_programme` and a `bass_management` model built from the `BM_*` settings.
With that model, the designer also returns `gain_reduction_db`.
