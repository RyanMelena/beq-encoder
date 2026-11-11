// cavern-dump: decode a DD+ Atmos (E-AC-3 + JOC) stream into its objects with the
// Cavern library (https://github.com/VoidXH/Cavern, see its LICENSE.md) and write them
// as a Dolby Atmos master file set (.atmos, .atmos.audio, .atmos.metadata), which
// DEE's TrueHD encoder (dee_dthd_encoder) accepts. Used by beq-encoder's DD+ Atmos -> TrueHD Atmos route.
//
//   cavern-dump info <in.ec3>
//   cavern-dump damf <in.ec3> <out.atmos> [updateRate=1536] [extraSamples=0]
//
// extraSamples renders past the end of the bitstream: Cavern's object path delays the
// audio (decoder + JOC filter bank), and without the extra samples the last ones still
// in its delay lines would be cut off. beq-encoder trims the lead and keeps the length.
using System;
using System.Diagnostics;

using Cavern;
using Cavern.Format;
using Cavern.Format.Common;
using Cavern.Format.Environment;
using Cavern.Format.Renderers;

static class Program {
    static int Main(string[] args) {
        if (args.Length < 2) {
            Console.Error.WriteLine("usage: cavern-dump info <in.ec3> | damf <in.ec3> <out.atmos> [updateRate] [extraSamples]");
            return 2;
        }
        string mode = args[0], input = args[1];
        Stopwatch sw = Stopwatch.StartNew();
        AudioReader reader = AudioReader.Open(input);
        reader.ReadHeader();  // Open() does not: without it Length is 0
        Console.WriteLine($"reader={reader.GetType().Name} channels={reader.ChannelCount} " +
                          $"rate={reader.SampleRate} length={reader.Length}");
        Renderer r = reader.GetRenderer();
        Console.WriteLine($"renderer={r.GetType().Name} objects={r.Objects.Count} hasObjects={r.HasObjects}");
        if (r is EnhancedAC3Renderer e && e.HasObjects) {
            Console.WriteLine($"dynamicObjects={e.DynamicObjects}");
        }
        if (mode == "info") {
            return 0;
        }
        if (mode != "damf" || args.Length < 3) {
            Console.Error.WriteLine("bad arguments");
            return 2;
        }
        if (!r.HasObjects) {
            Console.Error.WriteLine("the stream carries no objects (not DD+ Atmos)");
            return 3;
        }
        int updateRate = args.Length > 3 ? int.Parse(args[3]) : 1536;
        long extra = args.Length > 4 ? long.Parse(args[4]) : 0;
        Listener listener = new Listener() {
            SampleRate = reader.SampleRate,
            UpdateRate = updateRate,
            AudioQuality = QualityModes.Perfect,
        };
        listener.AttachSources(r.Objects);
        long length = reader.Length + extra;
        EnvironmentWriter writer = EnvironmentWriter.Create(args[2], Codec.DAMF, listener, length,
                                                            BitDepth.Int24, r);
        long done = 0, next = 0;
        while (done < length) {
            writer.WriteNextFrame();
            done += listener.UpdateRate;
            if (done >= next) {
                Console.WriteLine($"progress {Math.Min(done, length)} {length}");
                next += reader.SampleRate * 30L;
            }
        }
        writer.Dispose();
        Console.WriteLine($"written {length} samples in {sw.Elapsed.TotalSeconds:F1} s");
        return 0;
    }
}
