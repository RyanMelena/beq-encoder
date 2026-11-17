# How BEQ Encoder makes a BEQ track

This page explains what happens between **Add BEQ track** and the replaced MKV, and why each step exists.
The encode itself has its own page: [TrueHD encoding through dee_shim](truehd-encoding.md).

## Three decisions, then the work runs without you

1. **Track.** The scan probes the file with `mkvmerge -J`, `ffprobe`, and MediaInfo. The wizard preselects the best track that the support rules allow.
2. **Filter.** The wizard lists the film's filters, each with its curve, the bass it restores, and its level change at the low-end cutoff you pick.
3. **Options.** You confirm the output format and the default-track flag, then click **Add BEQ track**.

The encoder then filters the track, encodes it, checks it, and remuxes the file.
If any step fails, the source file stays untouched.

## Where the filter comes from

The film page lists every filter the encoder knows for the film, before any work starts.

- The BEQCatalogue's entries for the film come first. Each entry says which of the film's tracks it was made for, or which other mix of the film.
- An entry that says "no filters" is left out of the list.
- A filter the beqforge designer made for the film's file is listed with the catalogue's. You ask for one with **Design a filter** in the wizard, and it joins the list when the design ends.

The wizard preselects only a filter made for the chosen track.
It never preselects a filter made for another track or another mix.

## How the encoder picks the best track

The encoder ranks tracks by format first: lossless with objects, lossless channel-based, lossy with objects, then lossy.
Channel count breaks a tie, then the original-language flag, then the default flag.
The encoder skips commentary tracks, descriptive-audio tracks, and existing BEQ tracks.
The forced flag doesn't exclude a track: Matroska defines it for subtitles, and some remuxes set it on the main audio track.
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
   Every TrueHD track it copies keeps its own timestamps. Left to itself, mkvmerge re-times TrueHD that it reads from Matroska, and each run would move those tracks about 1/6 ms later.
3. The encoder verifies the track count, the track names, and the duration.
4. The encoder compares A/V sync and channel order with the original, and remuxes once if the BEQ track is more than 1 ms off.
5. `mkvpropedit` embeds the charts and the report, and the encoder checks the tracks again.
6. The encoder copies the permissions and the owner, then replaces the source in one atomic rename.

## One filter and one gain for every channel

The filter is a single cascade of RBJ biquads with the same coefficients as the catalogue, followed by the [high-pass floor](#the-high-pass-floor) when the job has one.
The encoder applies the filter to every channel: beds, LFE, and objects.
The filters are linear, so the result equals a filter on the bass-managed sub feed.

One gain, the same for every channel, brings the filtered peak to `TARGET_PEAK_DBFS`.
A gain that clips fails the job, because a clipped sample cannot equal the filtered source.

The encoder dithers a channel-based track to 24 bits with TPDF dither.
The encoder rounds an Atmos master to 24 bits without dither, an error of at most 0.5 LSB.
The reason is DEE: under `--as-is`, DEE codes a rounded master bit for bit, but codes a dithered master 1 to 4 LSB off.

## The high-pass floor

Some filters lift the bass far below what a subwoofer can play, such as 5 to 12 Hz.
The automatic gain must still make room for that lift, so the whole track gets quieter, including the bass that you can hear.
A high-pass floor removes the bass below a frequency that you choose, so the track needs less attenuation.
Set the floor to the lowest frequency that your subwoofers play, such as the tuning frequency of a ported sub, as miniDSP BEQ users do with a high-pass filter at the port tuning.

The floor is a 4th-order Butterworth high-pass filter: flat above the floor frequency, 3 dB down at it, and 24 dB per octave below it.
The encoder builds it as two biquads and adds them to the end of the BEQ cascade, on every channel that the BEQ filters.
The encode, the lossless check, and the gain estimate all run the same cascade, so the check still compares every sample with the filtered source.

`BEQ_HIGHPASS_HZ` sets the floor that the options of a new job start with, and you can change it for each job under **Options for the new track**.
`0`, the default, adds no floor, so the BEQ track is the filter as its source published it.
The track name, the tags, the report, and the charts record the floor, apart from the filter. See [what a job writes into the MKV](output.md).

What a floor gives back depends on the film and the filter. On a 30 s clip of Master and Commander with the catalogue filter by remixmark, the encoder measured these gains:

| Floor | Gain | Loudness of the BEQ track (the original: -12.0 LUFS) |
|---|---|---|
| None | -13.4 dB | -25.3 LUFS |
| 10 Hz | -7.5 dB | -19.5 LUFS |
| 15 Hz | -4.0 dB | -16.0 LUFS |
| 20 Hz | -1.9 dB | -13.9 LUFS |

The cost is the bass below the floor, which the BEQ restores and a system with more extension could play.

## The level change in the wizard

The wizard shows a **Level change** for each filter: the gain the encode would apply at `TARGET_PEAK_DBFS`.
When you open the wizard, the encoder measures every filter of the film at every low-end cutoff that the wizard offers, in one background job, from the 1 kHz analysis of the track described in [What the designer receives](#what-the-designer-receives).
It runs each filter, followed by the cutoff, over every channel of the analysis, and takes the highest filtered peak.
The presets are off, 5, 10, 15, 20, and 25 Hz. A custom cutoff is measured when you set it, which takes a few seconds once the track has been read.
The figures fill in as they arrive, and a change between presets shows its figure at once. You never wait for them to choose a filter.
A BEQ Forge design measures its own filters before it ends.
A BEQ filter only changes the bass, so that peak is close to the one the encode measures.
The estimate has two limits.
Content above 500 Hz is not in the analysis, so a track whose loudest peak is in the treble can need more attenuation than the estimate says.
For an Atmos track, the analysis is the channel presentation, while the encode filters the objects.
If the analysis cannot be made, the wizard shows no figure, and the encode measures the gain as usual.

## Hear the filters on a clip

**Make a BEQ clip** on the film page opens the wizard for a clip, which writes a short excerpt of the film to `CLIP_DIR`.
The clip holds the video, the original track, and a BEQ track for each filter you tick, up to four, so you can switch between them in your player at the same moment of the film.
Each BEQ track plays at the level the full BEQ track would get, and the film file does not change.
If that level cannot be measured, the clip fails instead of playing at its own level.
When the clip is ready, the film page offers **Add BEQ track** for each of its filters.

The start is the time you type, as `h:mm:ss`, `m:ss`, or seconds.
If you leave it empty, the encoder picks the window where the ticked filters add the most bass.
Once the level changes have been measured, the wizard suggests the heaviest bass passage and draws the bass over the running time, so you can pick another.
It runs each filter over the mono mix of the 1 kHz analysis and takes the window where the filtered mix differs most from the original.
The length is `CLIP_SECONDS`, unless you set another.

The encoder reads only the part of the film around the clip:

1. `ffmpeg` copies the video and the track from 15 seconds before the start to 15 seconds after the end. It seeks by the cues of the file, so a film on a network share is not read in full.
2. `mkvmerge` cuts the clip from the keyframe at or before the start.
3. The cut gives the first audio packet the time of the keyframe, 30 to 80 ms before the next packet. The bitstream is whole, but the track would show a gap after its first packet. The encoder moves that packet next to the second one, so the track follows on without a break.

Each BEQ track then takes the route of the full encode, with the same filter and the same checks: every sample of the track is the filtered original.
The A/V sync and channel order checks compare each BEQ track with the original track of the clip.
Clips run one at a time, in a queue of their own, so a clip does not wait for an encode.

**The level of a clip is the level of the whole film.**
A clip encoded on its own would get its own gain, which can be several decibels from the gain of the full encode.
The encoder uses the [gain estimate](#the-gain-estimate-on-the-review-page) instead, so the BEQ track of a clip plays as loud as the BEQ track of the film will.
If the clip itself peaks higher than the estimate, the encoder turns the clip down further, so that nothing clips. The clip list says which of the two set the level.
With **Fixed amount** as the level change, the clip uses that amount.
When the review has no estimate for a filter, the clip makes one, which reads the whole track once if the analysis is not cached.

## Timing matches the source track

The BEQ track starts at the same point on the timeline of the film as the source track.
The encoder reads that point from the first decoded frame of the source.
Whole milliseconds go into an `mkvmerge --sync` offset.
The rest, less than a millisecond, becomes leading digital silence.
The encoder measures any silence that DEE adds in front and compensates for it in the mux.

A decoder sometimes produces nothing for part of the timeline.
TrueHD frames before a restart point do that, and so does a gap between joined segments.
The encoder inserts the same length of digital silence, as a player does, and reports the gap in the job's log.
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
