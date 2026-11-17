# What a job writes into the MKV

A finished job adds one audio track, a set of Matroska tags, and three attachments to the MKV.

## Track

The BEQ track follows its source track and is not the default track unless `BEQ_TRACK_DEFAULT=true`.

`TRACK_NAME_TEMPLATE` sets the name. The default name gives the overall attenuation of the track and the format:

- `BEQ [-4.5 dB] TrueHD Atmos`
- `BEQ [0.0 dB] TrueHD 7.1`

The attenuation comes first because some players, such as Kodi, cut long track names short.

A name is at most 40 characters.
The encoder cuts a longer name at the last whole word that fits.
If the cut lands inside brackets, the encoder drops the brackets and their content.
The name does not give the filter source, the author, the [high-pass floor](how-it-works.md#the-high-pass-floor), or the source track. The [tags](#tags) record them.

The attenuation is the uniform gain that the encoder applied to every channel so that the filtered track does not clip.
The BEQ track keeps the dialogue level of its source, so this gain is the whole level difference between the BEQ track and the original.
When you play the BEQ track, raise the volume by that amount to match the original. For example, raise the volume by 4.5 dB for a track named `[-4.5 dB]`.
The `{gain}` placeholder gives the value. A custom template without `{gain}` leaves the value out of the name, but the `BEQ_GAIN_DB` tag still records it.

A custom template can add the `{source}` placeholder, which gives the filter source, such as `aron7awol`, and the `{highpass}` placeholder, which gives the floor, such as `HPF 15 Hz`.
A track without a floor gets nothing for `{highpass}`, and the name drops the comma or the brackets left empty.
The `BEQ_HIGHPASS_HZ` tag records the floor, whether or not the name gives it.

A BEQ track that an earlier version named keeps its name, such as `BEQ [-4.0 dB] TrueHD Atmos (remixmark, HPF 15 Hz)`.

A new BEQ track replaces the BEQ track that the film already has.
To keep both, clear **Replace an existing BEQ track** when you choose the filter.

## Tags

`write_tags` in `app/beq_encoder/pipeline/artifacts.py` writes these tags on the BEQ track. It omits a tag that has no value.

| Tag | Content |
|---|---|
| `BEQ_SOURCE` | The origin of the filter. The default is `catalogue`. |
| `BEQ_LABEL`, `BEQ_AUTHOR` | The label and the author of the filter. |
| `BEQ_DIGEST` | The digest of the filter entry. |
| `BEQ_AUDIO_TYPES` | The audio types that the filter entry names. |
| `BEQ_FILTERS` | The filters, as JSON, as their source published them. The high-pass floor is not one of them. |
| `BEQ_HIGHPASS_HZ`, `BEQ_HIGHPASS_ORDER` | The frequency and the order of the Butterworth high-pass floor that follows the filters. A track without a floor has neither tag. |
| `BEQ_GAIN_DB` | The uniform gain applied after the filter. |
| `BEQ_SOURCE_TRACK` | The label and the codec of the source track. |
| `BEQ_OUTPUT` | The codec of the BEQ track. |
| `BEQ_CHECK` | How the encoder verified the encode. |
| `BEQ_STRATEGY`, `BEQ_DESIGNER_RELEASE`, `BEQ_DESIGN_VERSION`, `BEQ_DESIGN_SETTINGS` | The strategy, the designer release, the design version, and the design settings of a designed filter. |
| `BEQ_ENCODER`, `BEQ_ENCODER_REVISION` | The encoder version and the encoder revision that made the track. |
| `BEQ_CHART_SPECTRUM`, `BEQ_CHART_HEATMAP`, `BEQ_REPORT` | The file names of the attachments. |

`BEQ_CHECK` starts with "sample-exact:". For a DD+ Atmos source it starts with "objects decoded by Cavern, encoded sample-exact from that decode:".

The library reads these tags to find [stale BEQ tracks](library.md#stale-beq-tracks).

## Charts

Every encode ends with two charts.
The encoder measures both from the original track and from the encoded BEQ track, decoded back. Neither chart is a model.

| Chart | Range | Content |
|---|---|---|
| Peak and average spectrum | 1 to 160 Hz | The original, dashed, and the BEQ, solid. The filter table, the high-pass floor, the gain, the sample peaks, and the check result are printed beside it. |
| Bass over the running time | 1 to 60 Hz | A heat map of the whole film, the original above the BEQ. The scale is fixed at -95 to -25 dBFS, so you can compare films. |

Both charts use the mono mix of the designer and print their method on the image.
The encoder removes the gain of the BEQ track from the "after" curves, so the charts show the BEQ alone.

If the encoder cannot make a chart, it logs the error and still writes the BEQ track.
To turn the charts off, set `BEQ_CHARTS=false`.

## Attachments

| Attachment | Content |
|---|---|
| `beq-<id>-spectrum.png` | The spectrum chart. |
| `beq-<id>-heatmap.png` | The heat map. |
| `beq-<id>.json` | A machine-readable report of the job. A track with a floor has `filter.highpass`, with the type, the order, and the frequency of the floor. |

A player add-on can find the attachments through the `BEQ_CHART_SPECTRUM`, `BEQ_CHART_HEATMAP`, and `BEQ_REPORT` tags.
When a BEQ track is replaced, its attachments are replaced too. The encoder keeps every other attachment, such as fonts and cover art.

## Clips

A clip is a new MKV in `CLIP_DIR`. The film itself does not change. See [Hear the filters on a clip](how-it-works.md#hear-the-filters-on-a-clip).

The file is named for the film and the time the clip starts, such as `Pacific Rim (2013) - BEQ demo 01.21.12.mkv`.
A second clip with the same name gets ` (2)`, ` (3)`, and so on: a clip never replaces a file.

| Track | Content |
|---|---|
| Video | The film's first video track, cut on the keyframe at or before the start. |
| Original | The source track, copied bit for bit, with its name and language. It is the default track. |
| One BEQ track for each filter | Named and tagged as the full encode names and tags its track, such as `BEQ [-13.4 dB] TrueHD Atmos`. The gain in the name is the level the whole film gets, unless the clip itself peaks higher. |

Every audio track of a clip starts at the same timestamp.
A clip has no chapters, no subtitles, no charts, and no attachments.
