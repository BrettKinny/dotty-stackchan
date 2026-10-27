#!/usr/bin/env python3
"""Local review exports from Dotty case evidence. AI-assisted: OpenAI Codex (GPT-6)."""

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


GATES = {"visual", "privacy", "captions", "music", "context"}


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value):
        raise ValueError("case/export name must be a simple identifier")
    return value


def inside(path, root):
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes session directory")
    return path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path, text):
    with path.open("x", encoding="utf-8") as output:
        output.write(text)


def execute(command):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)


def media_duration(source):
    data = json.loads(execute(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                              "-of", "json", str(source)]).stdout)
    if not {"video", "audio"}.issubset({s["codec_type"] for s in data["streams"]}):
        raise ValueError("source requires video and audio")
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid source duration")
    return duration


def stamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milliseconds = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def captions(transcript, offset, start, end):
    """Convert response-relative segments to clip-relative SRT, retaining provenance."""
    if not all(math.isfinite(n) for n in (offset, start, end)) or offset < 0 or start < 0 or end <= start:
        raise ValueError("invalid caption timing")
    cues = []
    for segment in transcript.get("segments", []):
        source_start = float(segment["start"]) + offset
        source_end = float(segment["end"]) + offset
        if not all(math.isfinite(n) for n in (source_start, source_end)) or source_end <= source_start:
            raise ValueError("invalid transcript segment timing")
        begin, finish = max(start, source_start), min(end, source_end)
        text = " ".join(str(segment["text"]).split())
        if finish > begin and text:
            cues.append({"start": begin - start, "end": finish - start, "text": text,
                         "source_start": source_start, "source_end": source_end})
    srt = "\n\n".join(f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}"
                      for i, c in enumerate(cues, 1))
    return srt + ("\n" if srt else ""), cues


def file_digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def url(path, base):
    return quote(os.path.relpath(path, base), safe="/")


def render_index(manifests, output):
    rows = []
    for path in manifests:
        record = load(path)
        folder = path.parent
        evidence = record["source"]
        root = Path(record["session"])
        source = root / evidence["file"]
        result = root / evidence["result_file"]
        links = [("Video", folder / "clean.mp4"), ("Thumbnail", folder / "thumbnail.jpg"),
                 ("Source", source), ("Case evidence", result), ("Provenance", path)]
        if (folder / "captions.srt").exists():
            links.append(("SRT captions", folder / "captions.srt"))
        anchor = " · ".join(f'<a href="{url(p, output.parent)}">{label}</a>' for label, p in links)
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in [
            record["title"], record["case_id"], record["verdict"], record["status"],
            ", ".join(record["pending_reviews"]) or "all gates acknowledged",
            f'{evidence["start_seconds"]:.3f}–{evidence["end_seconds"]:.3f}s',
            record["commit"], record["export_state"]]) + f"<td>{anchor}</td></tr>")
    page = ('<!doctype html><meta charset="utf-8"><title>Dotty local clip review</title>'
            '<style>body{font:16px system-ui;margin:2em;background:#fafafa}table{border-collapse:collapse}'
            'td,th{border:1px solid #ccc;padding:.6em;vertical-align:top}a{white-space:nowrap}</style>'
            '<h1>Dotty local clip review</h1><p>AI-assisted exports by OpenAI Codex (GPT-6). '
            'Human review required before posting. Test verdicts are separate from clip readiness. '
            'Captions are provisional unless their review gate is acknowledged. Source recordings '
            'retain the full exchange; trimming and audio normalization apply only to exports.</p>'
            '<table><tr><th>Title suggestion</th><th>Case</th><th>Verdict</th><th>Review group</th>'
            '<th>Pending reviews</th><th>Source interval</th><th>Commit</th><th>Export</th><th>Files</th></tr>'
            + "".join(rows) + '</table>')
    write_new(output, page)
    return output


def crop_region(value):
    """`W:H:X:Y` in source pixels. Portrait only: the region is scaled to fill
    1080x1920, so a landscape region would be stretched."""
    if not re.fullmatch(r"\d+:\d+:\d+:\d+", value or ""):
        raise ValueError("crop must be W:H:X:Y in source pixels")
    width, height, _x, _y = (int(part) for part in value.split(":"))
    if width < 2 or height < 2 or abs(width / height - 9 / 16) > .02:
        raise ValueError("crop region must be 9:16 portrait")
    return value


def export(session, case_id, *, start=0, end=None, name=None, title=None, reviewed=(),
           include_captions=True, crop=None):
    session = session.resolve(strict=True)
    case = inside(session / "cases" / identifier(case_id), session / "cases")
    source = inside(case / "raw.mp4", session)
    result_path = inside(case / "result.json", session)
    result = load(result_path)
    duration = media_duration(source)
    end = duration if end is None else end
    if not all(math.isfinite(n) for n in (start, end)) or not 0 <= start < end <= duration:
        raise ValueError("clip interval must be within source duration")
    if crop is not None:
        crop = crop_region(crop)
    acknowledged = set(reviewed)
    if acknowledged - GATES:
        raise ValueError("unknown review gate")
    pending = sorted(GATES - acknowledged)
    verdict = str(result.get("verdict", "INCONCLUSIVE"))
    status = "failures" if verdict == "FAIL" else "needs-review" if pending else "ready-for-review"
    name = identifier(name or case_id + "-" + datetime.now(timezone.utc).strftime("%H%M%S%f"))
    destination = inside(session / "clips" / status / name, session)
    # Exclusive directory reservation makes every export immutable, including failed attempts.
    destination.mkdir(parents=True, exist_ok=False)
    record = {"ai_assistance": "OpenAI Codex (GPT-6)", "session": str(session),
              "case_id": case_id, "feature": result.get("case_id", case_id),
              "title": title or result.get("case", {}).get("prompt", case_id),
              "verdict": verdict, "status": status, "pending_reviews": pending,
              "reviewed": sorted(acknowledged), "commit": result.get("commit", "unknown"),
              "created": datetime.now(timezone.utc).isoformat(), "export_state": "incomplete",
              "source": {"file": str(source.relative_to(session)),
                         "result_file": str(result_path.relative_to(session)),
                         "sha256": file_digest(source), "start_seconds": start, "end_seconds": end,
                         "case_started": result.get("started"), "duration_seconds": duration},
              "layout": (f"1080x1920 cropped from source region {crop}" if crop
                         else "1080x1920 fitted; entire source frame retained"),
              "audio": "original audio with loudness normalization; no replacement soundtrack",
              "captions": {"state": "absent", "source": "local response.json transcription"}}
    try:
        execute(["ffmpeg", "-nostdin", "-n", "-v", "error", "-ss", str(start), "-i", str(source),
                 "-t", str(end - start), "-map", "0:v:0", "-map", "0:a:0", "-vf",
                 (f"crop={crop},scale=1080:1920:flags=lanczos,setsar=1" if crop else
                  "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                  "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(destination / "clean.mp4")])
        execute(["ffmpeg", "-nostdin", "-n", "-v", "error", "-ss", str(min(2, (end-start)/2)),
                 "-i", str(destination / "clean.mp4"), "-frames:v", "1", str(destination / "thumbnail.jpg")])
        transcript = inside(case / "response.json", session)
        if include_captions and transcript.exists():
            if "response_offset" not in result:
                record["captions"]["state"] = "blocked: missing response_offset; cannot align captions"
            else:
                offset = float(result["response_offset"])
                srt, cues = captions(load(transcript), offset, start, end)
                write_new(destination / "captions.srt", srt)
                record["captions"].update(state="reviewed" if "captions" in acknowledged else "provisional",
                                          response_offset=offset, cues=cues)
        record["export_state"] = "complete"
    except Exception as exc:
        record["error"] = str(exc)
        raise
    finally:
        write_new(destination / "manifest.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        render_index([destination / "manifest.json"], destination / "index.html")
    return destination


def index(session):
    session = session.resolve(strict=True)
    clips = inside(session / "clips", session)
    clips.mkdir(exist_ok=True)
    manifests = [p for group in ("ready-for-review", "needs-review", "failures")
                 for p in sorted(clips.glob(f"{group}/*/manifest.json"))]
    output = clips / ("index-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".html")
    return render_index(manifests, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export")
    p.add_argument("session", type=Path)
    p.add_argument("case_id")
    p.add_argument("--start", type=float, default=0)
    p.add_argument("--end", type=float)
    p.add_argument("--name")
    p.add_argument("--title")
    p.add_argument("--reviewed", choices=sorted(GATES), action="append", default=[])
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--crop", help="W:H:X:Y portrait source region to fill the 9:16 frame "
                                  "(default: fit the whole frame with padding)")
    p = sub.add_parser("index")
    p.add_argument("session", type=Path)
    args = parser.parse_args()
    if args.command == "index":
        print(index(args.session))
    else:
        print(export(args.session, args.case_id, start=args.start, end=args.end, name=args.name,
                     title=args.title, reviewed=args.reviewed, include_captions=not args.no_captions,
                     crop=args.crop))


if __name__ == "__main__":
    main()
