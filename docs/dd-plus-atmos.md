# DD+ Atmos through Cavern

This page explains how BEQ Encoder keeps the objects of a DD+ Atmos track, and how far to trust the result.

## Why Cavern

No Dolby tool decodes DD+ Atmos (E-AC-3 with JOC) to its objects.
`ddp_decode` in DEE outputs channels only, and `ffmpeg` outputs only the 5.1 or 7.1 bed.
A BEQ track made from the bed alone drops the objects, which makes it a lossy conversion.

[Cavern](https://github.com/VoidXH/Cavern) decodes the objects.
`cavern-dump`, in `cavern/CavernDump`, writes the objects as a Dolby Atmos master file set.
`dee_dthd_encoder` accepts that file set as it accepts the output of `truehdd`.

## How close Cavern is to the Dolby decode

Cavern is not a Dolby decoder, so its objects differ from a Dolby decode. The difference is small where BEQ acts.

| Band | Agreement with `ddp_decode` |
|---|---|
| Below 120 Hz | 94 to 120 dB SNR |
| Up to 500 Hz | 97 to 108 dB SNR |
| 0.5 to 4 kHz | 66 to 82 dB SNR |
| LFE | bit-identical |

Above 4 kHz the agreement is lower, because the QMF and JOC code in Cavern is not the code that Dolby uses.

The BEQ track says so itself.
The job page, the report, and the `BEQ_CHECK` tag label the track "objects decoded by Cavern, encoded sample-exact from that decode".
The samples are exactly the objects that Cavern decoded, with the BEQ applied.

## How the encoder aligns the decode

The LFE from Cavern lines up with an ordinary E-AC-3 decode.
The objects come 577 samples later, because of the delay of the JOC filter bank.

`cavern-dump` renders 1,536 samples past the end of the stream, so that the last samples of the objects leave the delay lines.
The encoder then trims the lead-in of the objects and keeps exactly the length of the stream.

## What the encoder checks on every film

The job fails if any of these checks fail:

- The LFE equals the LFE that `ffmpeg` decodes: correlation at least 0.99 at lag 0, and level within 0.1 dB. Correlation alone accepts a level change, and the LFE is where BEQ acts.
- The sum of the objects tracks the main channels that `ffmpeg` decodes, with correlation at least 0.99. This check catches a Cavern build whose timing has changed.
- The decode contains exactly the samples of the stream.
- The encode passes [the checks for TrueHD Atmos](truehd-encoding.md#the-extra-checks-for-truehd-atmos), measured against the kept Cavern decode.

## Build the image without Cavern

To leave Cavern out, build the image with an empty `--build-arg CAVERN_COMMIT=`.
The encoder then refuses DD+ Atmos tracks.

The Cavern licence allows free use and modification.
It forbids selling Cavern or showing advertisements with it, and asks for credit with a link when you use Cavern publicly or commercially.
An image that you publish carries these terms. The image holds the licence at `/opt/cavern-dump/CAVERN-LICENSE.md`.
