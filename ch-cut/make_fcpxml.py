#!/usr/bin/env python3
"""
CH Cut - step 1b: sync + FCPXML export

Reads a camera video and a separately recorded audio file, finds the sync
offset (using sync_audio.py in the same folder) and writes an .fcpxml file
for Final Cut Pro.

In Final Cut Pro the result is:
  - the video clip (its own camera audio is muted)
  - the separate audio as an independent connected clip, already synced
Original files are only referenced, never modified or re-encoded.

Usage:
    python3 make_fcpxml.py VIDEO AUDIO [--out FILE.fcpxml] [--force]
"""
import argparse
import json
import math
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
import xml.etree.ElementTree as ET

import sync_audio as sa

FCPXML_VERSION = "1.10"
AUDIO_RATES = {32000: "32k", 44100: "44.1k", 48000: "48k", 88200: "88.2k",
               96000: "96k", 176400: "176.4k", 192000: "192k"}


def probe(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", str(path)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        sys.exit("ffprobe failed on %s:\n%s" % (path, p.stderr.decode(errors="replace")))
    return json.loads(p.stdout)


def first_stream(info, kind):
    for s in info.get("streams", []):
        if s.get("codec_type") == kind:
            return s
    return None


def duration_of(info, stream):
    for src in (stream, info.get("format", {})):
        d = src.get("duration") if src else None
        if d not in (None, "N/A"):
            return Fraction(d).limit_denominator(1000000)
    sys.exit("Could not read duration")


def rotation_of(stream):
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"])))
    r = (stream.get("tags") or {}).get("rotate")
    return int(r) if r else 0


def t(frac):
    """Fraction of seconds -> FCPXML time string (rational, reduced)."""
    frac = Fraction(frac)
    if frac == 0:
        return "0s"
    if frac.denominator == 1:
        return "%ds" % frac.numerator
    return "%d/%ds" % (frac.numerator, frac.denominator)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("audio")
    ap.add_argument("--out")
    ap.add_argument("--no-rotate", action="store_true",
                    help="ignore the phone's rotation flag (keep 3840x2160)")
    ap.add_argument("--force", action="store_true",
                    help="write the file even if sync confidence is low")
    args = ap.parse_args()

    vpath = Path(args.video).expanduser().resolve()
    apath = Path(args.audio).expanduser().resolve()
    out = Path(args.out).expanduser() if args.out else Path.home() / "Desktop" / (vpath.stem + ".fcpxml")

    # ---- 1. sync -------------------------------------------------------
    v = sa.load_mono(str(vpath))
    e = sa.load_mono(str(apath))
    lag, conf = sa.gcc_phat(v, e)
    offset = Fraction(lag, sa.SR)  # audio starts at +offset s on the video timeline
    print("offset: %+.4f s   confidence: %.2f (%s)" % (float(offset), conf, sa.quality_label(conf)))
    drift = sa.drift_check(v, e, lag)
    vals = [d["extra_offset_ms"] for d in drift if d["confidence"] >= 1.8]
    if len(vals) >= 2:
        spread = max(vals) - min(vals)
        print("drift spread: %.2f ms" % spread)
    if conf < 1.8 and not args.force:
        sys.exit("Sync confidence is low - not writing the file. "
                 "Check the files, or run again with --force.")

    # ---- 2. media info -------------------------------------------------
    vinfo, ainfo = probe(vpath), probe(apath)
    vs, as_ = first_stream(vinfo, "video"), first_stream(ainfo, "audio")
    if vs is None or as_ is None:
        sys.exit("Video or audio stream not found")

    width, height = int(vs["width"]), int(vs["height"])
    if abs(rotation_of(vs)) in (90, 270) and not args.no_rotate:
        width, height = height, width  # iPhone portrait video
    num, den = [int(x) for x in vs["r_frame_rate"].split("/")]
    fps = Fraction(num, den)
    frame_dur = Fraction(den, num)  # seconds per frame
    vdur = duration_of(vinfo, vs)
    adur = duration_of(ainfo, as_)
    arate = int(as_["sample_rate"])
    achan = int(as_.get("channels", 2))
    if arate not in AUDIO_RATES:
        sys.exit("Unsupported audio sample rate: %d" % arate)

    # video length rounded DOWN to whole frames
    vframes = math.floor(vdur * fps)
    seq_dur = vframes * frame_dur

    # ---- 3. place the external audio ----------------------------------
    # audio time x  <->  video time x + offset  ... audio_time = video_time - offset
    # (offset < 0: audio started before the video -> skip the pre-roll)
    if offset <= 0:
        a_start = Fraction(round(-offset * arate), arate)  # inside the audio file
        a_offset = Fraction(0)                              # on the video clip
    else:
        a_start = Fraction(0)
        a_offset = Fraction(round(offset * arate), arate)
    a_len = min(seq_dur - a_offset, adur - a_start)
    if a_len <= 0:
        sys.exit("Audio and video do not overlap")

    # ---- 4. build the XML ---------------------------------------------
    fcpxml = ET.Element("fcpxml", version=FCPXML_VERSION)
    res = ET.SubElement(fcpxml, "resources")
    if den == 1001:
        fd_str = "1001/%ds" % num
    else:
        fd_str = "%d/%ds" % (den * 100, num * 100)
    fps_tag = {Fraction(30000, 1001): "2997", Fraction(24000, 1001): "2398",
               Fraction(60000, 1001): "5994"}.get(fps, str(int(fps)))
    fmt_name = "FFVideoFormat%dx%dp%s" % (width, height, fps_tag)
    hlg = vs.get("color_transfer") == "arib-std-b67"
    color = "9-18-9 (Rec. 2020 HLG)" if hlg else "1-1-1 (Rec. 709)"
    ET.SubElement(res, "format", id="r1", name=fmt_name, frameDuration=fd_str,
                  width=str(width), height=str(height), colorSpace=color)
    cam_a = first_stream(vinfo, "audio")
    cam_rate = AUDIO_RATES.get(int(cam_a["sample_rate"]), "48k") if cam_a else "48k"
    cam_chan = str(int(cam_a["channels"])) if cam_a else "2"
    va = ET.SubElement(res, "asset", id="r2", name=vpath.stem, start="0s",
                       duration=t(vdur), hasVideo="1", hasAudio="1", format="r1",
                       audioSources="1", audioChannels=cam_chan, audioRate=cam_rate)
    ET.SubElement(va, "media-rep", kind="original-media", src=vpath.as_uri())
    aa = ET.SubElement(res, "asset", id="r3", name=apath.stem, start="0s",
                       duration=t(adur), hasAudio="1", audioSources="1",
                       audioChannels=str(achan), audioRate=AUDIO_RATES[arate])
    ET.SubElement(aa, "media-rep", kind="original-media", src=apath.as_uri())

    lib = ET.SubElement(fcpxml, "library")
    ev = ET.SubElement(lib, "event", name="CH Cut")
    proj = ET.SubElement(ev, "project", name=vpath.stem)
    seq = ET.SubElement(proj, "sequence", format="r1", duration=t(seq_dur),
                        tcStart="0s", tcFormat="NDF", audioLayout="stereo",
                        audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    vclip = ET.SubElement(spine, "asset-clip", ref="r2", offset="0s",
                          name=vpath.stem, start="0s", duration=t(seq_dur),
                          format="r1", tcFormat="NDF")
    # camera audio is only the sync reference: mute it
    ET.SubElement(vclip, "adjust-volume", amount="-96dB")
    ET.SubElement(vclip, "asset-clip", ref="r3", lane="-1", offset=t(a_offset),
                  name=apath.stem, start=t(a_start), duration=t(a_len),
                  tcFormat="NDF", audioRole="dialogue")

    ET.indent(fcpxml, space="  ")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(ET.tostring(fcpxml, encoding="utf-8"))
    print("written:", out)


if __name__ == "__main__":
    main()
