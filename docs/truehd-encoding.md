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
dee_shim keeps the elements in the order of the master, and unused encoded elements are silent.
The check accepts the elements in any order.

**With `--as-is`,** the stream also ends at the last sample of the input.
Without that, DEE pads the input with silence to a whole block of 512 samples.

DEE 6.7 applies no surround attenuation by default and trims nothing.
DEE writes `beq.mlp.log.html` and `beq.mlp.mll` next to its output, and the folder passed as `--temp-dir` must exist.

## Quiet Atmos objects

An Atmos object can be quieter than the silence threshold of DEE, about 1.5 LSB.
DEE admits an object to a cluster by a weight, and the preprocessor of DEE leaves that weight at zero for such a block.
Before dee_shim 0.2, such an object had no cluster of its own, and DEE mixed it into the cluster of a neighbour.
An onset from digital silence came back 1 LSB off ([dee-shim issue 4](https://github.com/truehdd/dee-shim/issues/4)).
dee_shim 0.2 gives the object a weight, and the object comes back exact.

## How DEE codes object positions

DEE codes the positions themselves exactly, within float formatting. Three rules decide when a position takes effect.

1. DEE codes one metadata update per 1536-sample frame, which is 32 ms at 48 kHz. DEE counts frames from the first sample of the programme. The update holds the state after the last update inside the frame, so if one frame holds two updates of an object, only the last one survives.
2. dee_shim 0.2 codes that update at its sample in the master, with its ramp. If the frame holds updates at different samples, the update goes at the one that moves the most elements. On its own, DEE codes every update at the start of its frame, so an update can play up to 32 ms early ([dee-shim issue 3](https://github.com/truehdd/dee-shim/issues/3)).
3. DEE sends no updates for an object while the object is digitally silent. DEE writes the default position (-1, 1, 0) at the start of the programme, and sends the current position in the frame before the audio of the object resumes. No audible sample changes.

The release of truehdd that the image ships, 0.6.2, leaves out the OAMD block offset when it decodes.
It reads each update of the encode only to within an access unit (40 samples) of the start of its frame.
It reads some updates of a disc 32 samples (0.7 ms) early, so the master that DEE receives carries that error. On the first 6 minutes of Edge of Tomorrow, 397 of 1,788 updates are early.
truehdd commit 215b3ba fixes both. No release includes it yet.

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
- In every 1536-sample frame where an encoded object has audio, the position of the object matches the source position in effect at the last sample of the frame. The tolerance is 1/62 room units, half a step of the coded position.
- Each move of an encoded object in such a frame starts at a sample where the source updates an element.

How much of the timing the check can see depends on the truehdd that decodes the encode. The job log names it.

| truehdd | Where the check reads the encoded position | Update times |
|---|---|---|
| 0.6.2, in the image | Mid-frame | Not compared: truehdd reads them only to the frame |
| A release after 0.6.2, or a build at least 14 commits past it | At the last sample of the frame | Compared to the sample |

The position check fails on one wrong position and on any update that arrives one frame late.
Read to the sample, it also fails on an update at the start of its frame, which is the timing of DEE on its own.
An event without a `samplePos` happens at the time of the previous event, as Dolby and Cavern write it.

The job page and the JSON report record these results. The `BEQ_CHECK` tag records the first three.

- The largest error of each element, in LSB.
- The number of object-frames whose position the check compared.
- `update_times`: `sample` or `frame`, depending on how the decoder reads update times.
- `max_update_shift`, only when read to the sample: the largest distance between a move of an object and its own update in the source. The shift is not zero only when one frame holds updates at different samples.
