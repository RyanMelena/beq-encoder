# TrueHD encoding through dee_shim

This page explains how BEQ Encoder gets a sample-exact TrueHD stream out of the Dolby Encoding Engine (DEE) 6.7, and how it proves the result.

## Why DEE, and why dee_shim

Only DEE can create a TrueHD Atmos stream.
The stream carries Evolution frames, and HMAC-SHA-256 protection words in those frames cover the audio of each access unit.
`truehdd decode --evo-key` shows the words in disc streams and in the output of DEE. A home-made encoder cannot sign its output.

The TrueHD encoder of DEE 6.7 is `dee_dthd_encoder`.
Dolby ships it for macOS only, and [dthd-mac2linux](https://github.com/truehdd/dthd-mac2linux) turns the macOS build into a Linux program.

On its own, `dee_dthd_encoder` changes the audio. [dee_shim](https://github.com/truehdd/dee-shim) runs it with switches that its command line lacks.
dee_shim patches the private in-memory copy of the encoder and changes no DEE file.

## The command

Every encode runs this command:

```
dee_shim dthd_encoder --binary $DEE_PATH/dee_dthd_encoder --as-is on --drc none \
	[--oamd-as-is on] -- \
	--presentation 2ch:drc_default_on=0 \
	--presentation 6ch|8ch|atmos:number_of_elements=16:legacy_authoring=0 \
	--input-format wav|atmos_mezz -i <filtered input> \
	--loudness-management skip:dialnorm=<level of the source> \
	--progress 1 --overwrite 1 -o beq.mlp --temp-dir <work>/dee-temp
```

A 2.0 source has only the `2ch:drc_default_on=0` presentation.
The encoder passes `--oamd-as-is on` for Atmos input only.

| Switch | Effect |
|---|---|
| `--as-is on` | DEE codes every sample as read. |
| `--oamd-as-is on` | DEE codes the object metadata as authored. |
| `--drc none`, `2ch:drc_default_on=0` | Every presentation reports "DRC on by default: false" in `truehdd info`. |
| `--loudness-management skip:dialnorm=N` | DEE writes N unchanged. The 2-channel downmix shows N - 6, as on discs. |

**Without `--as-is`,** DEE changes every sample above 0.75 of full scale by 1 LSB. Dolby calls this the core limiter.
DEE also regroups an Atmos programme into 16 elements of its own and appends 40 frames.
With `--as-is`, every channel and every element decodes bit-exact, with no offset and no added frames.
The encoder therefore holds back no headroom for DEE, and `TARGET_PEAK_DBFS` applies on every route.

**With `--oamd-as-is`,** each source element gets an encoded element of its own.
The encoded element IDs are a permutation of the source IDs, and unused encoded elements are silent.

DEE 6.7 applies no surround attenuation by default and trims nothing.
DEE writes `beq.mlp.log.html` and `beq.mlp.mll` next to its output, and the folder passed as `--temp-dir` must exist.

## Why the image patches dee_shim

dee_shim v0.1.0 mishandles an Atmos object that is quieter than the silence threshold of DEE, about 1.5 LSB.
DEE admits an object to a cluster by a weight, and the preprocessor of DEE leaves that weight at zero for such a block.
The object then has no cluster of its own, and DEE mixes it into the cluster of a neighbour.
An onset from digital silence comes back 1 LSB off.

The image builds dee_shim with `docker/dee-shim/quiet-weight.patch`.
The patch gives a quiet object a weight below the threshold of DEE, and the object comes back exact.
The fix is filed upstream as [dee-shim issue 4](https://github.com/truehdd/dee-shim/issues/4).
Delete the patch when a dee_shim release includes the fix.

## How DEE codes object positions

DEE codes the positions themselves exactly, within float formatting. Two rules of DEE change when a position takes effect.

1. DEE codes every position update at the start of a 1536-sample frame, which is 32 ms at 48 kHz. DEE counts frames from the first sample of the programme. An update inside a frame moves to the start of that frame, so the update can play up to 32 ms early. If one frame holds two updates, only the last update survives.
2. DEE sends no updates for an object while the object is digitally silent. DEE writes the default position (-1, 1, 0) at the start of the programme, and sends the current position in the frame before the audio of the object resumes. No audible sample changes.

The early updates are filed upstream as [dee-shim issue 3](https://github.com/truehdd/dee-shim/issues/3).

## The check that every encode must pass

The encoder checks every BEQ track against the BEQ-filtered source over the full length of the track.
A failed check fails the job, and the source file stays untouched.

Every sample must equal `sosfilt(source) × gain` at 24 bits.
The tolerance for a channel-based track is 1.5 LSB, which covers the TPDF dither and the rounding.
The tolerance for TrueHD Atmos is 0.5 LSB, which covers the rounding alone.

## The extra checks for TrueHD Atmos

`truehdd` decodes the encoded track, and the check compares that decode with the kept, unfiltered decode of the source.
The check computes the BEQ again on its own, independent of the copy that DEE encoded.
The job fails unless all of these conditions hold:

- The bed is the same. The encode has at most 16 elements, has the full length, and aligns with the source. Any frames that DEE adds before or after the programme are digital silence.
- One encoded element carries each source element that has audio. The correlation over the whole programme is at least 0.99, or at least 0.98 for a Cavern decode. Every sample of the element is within 0.5 LSB. Every other encoded element is digitally silent.
- In every 1536-sample frame where an encoded object has audio, the position of the object matches the source. The check compares the position at mid-frame with the source position in effect at the last sample of the frame. The tolerance is 1/62 room units, half a step of the coded position.

The position check fails on one wrong position and on any update that arrives one frame late.
An event without a `samplePos` happens at the time of the previous event, as Dolby and Cavern write it.

The job page, the JSON report, and the `BEQ_CHECK` tag record three results:

- The largest error of each element, in LSB.
- The number of object-frames whose position the check compared.
- The largest lead that DEE gave an update.
