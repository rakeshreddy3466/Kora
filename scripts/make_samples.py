#!/usr/bin/env python3
"""Generate Ericsson-style sample material that exercises every duplicate class
and every versioning path. All content is synthetic.

  samples/
    principals.json, sources.json
    sharepoint/network-team/   system of record (site library)
    onedrive/bob/              departing employee's OneDrive (downloads, copies)
    play/                      Ericsson Play (re-encoded recording)

Scenarios
  Network Architecture 1/1551-LZA 701 0001 Uen  revisions PA1 -> PA2 -> A -> B
  "(1).pdf" / "(2).pdf" download copies           EXACT_COPY (one on legal hold)
  re-saved PDF with new metadata                  CONTAINER_VARIANT
  5G Core Overview shares 6 slides (own footer)   PARTIAL_OVERLAP via body hash
  3-page excerpt of rev A                         CONTAINED_SUBSET
  re-typed deck, same words, new layout           NEAR_DUPLICATE
  Team_Update / _Final / _Final_v2                filename-based versions
  Onboarding.pptx (+copies) and _v2               PPTX exact copies + version
  architecture review recording + Teams VTT       distillation, frame->slide links
  480p re-encode on Play                          MEDIA_VARIANT
  lab session recording without transcript        blocked: not distillable
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas

W, H = 960, 540
BLUE = HexColor("#1f5fa8")
DOC_NA = "1/1551-LZA 701 0001 Uen"
DOC_5G = "1/1551-LZA 701 0002 Uen"

# ----------------------------------------------------------------------------- slide content
BASE = {
    "agenda": ("Agenda", ["Current topology", "Design principles", "Transport layer", "Core integration",
                          "Migration plan", "Decisions and next steps"]),
    "topology_v1": ("Current topology", ["Two regional aggregation sites", "Single-homed access rings",
                                         "Legacy MPLS core"], ["Access", "Aggregation", "Core"]),
    "topology_v2": ("Current topology", ["Two regional aggregation sites", "Single-homed access rings",
                                         "Legacy MPLS core", "New edge data centre in Karlskrona"],
                    ["Access", "Aggregation", "Edge DC", "Core"]),
    "principles": ("Design principles", ["Automate everything that is repeated", "Dual-homed aggregation",
                                         "Segment routing in the transport core", "Observability by default"]),
    "transport_typo": ("Transport layer", ["Lattency budget 5 ms end to end", "100G uplinks from aggregation",
                                           "Timing via PTP grandmasters"]),
    "transport": ("Transport layer", ["Latency budget 5 ms end to end", "100G uplinks from aggregation",
                                      "Timing via PTP grandmasters"]),
    "core": ("Core network integration", ["5G core user plane at the edge", "Control plane centralised",
                                          "N3/N9 interfaces over segment routing"], ["gNB", "UPF", "SMF", "AMF"]),
    "migration": ("Migration plan", ["Phase 1: lab validation", "Phase 2: one pilot region",
                                     "Phase 3: nationwide rollout"]),
    "decisions": ("Decisions and next steps", ["Decision: adopt dual-homed aggregation",
                                               "Action: Bob Berg to validate lab results by June",
                                               "Action: Alice Andersson to update the HLD"]),
    "risks": ("Risks", ["Vendor lead times for 100G optics", "PTP accuracy on long rings",
                        "Staff availability during summer"]),
    "timeline": ("Rollout timeline", ["Q3 2026: pilot region live", "Q4 2026: 40% of sites",
                                      "Q2 2027: complete"]),
}

REVISIONS = {
    "PA1": ["agenda", "topology_v1", "principles", "transport_typo", "core", "migration", "decisions"],
    "PA2": ["agenda", "topology_v2", "principles", "transport_typo", "core", "migration", "decisions", "risks"],
    "A":   ["agenda", "topology_v2", "principles", "transport", "core", "migration", "decisions", "risks"],
    "B":   ["topology_v2", "principles", "transport", "migration", "core", "decisions", "risks", "timeline"],
}

FIVE_G = [("title5g", None), ("principles", None), ("transport", None), ("core", None), ("migration", None),
          ("u_slicing", ("Network slicing", ["Slice per enterprise customer", "SLA monitoring per slice",
                                             "Automated slice lifecycle"])),
          ("decisions", None), ("risks", None),
          ("u_ops", ("Operations model", ["NOC follows the sun", "Runbooks as code", "Weekly change board"]))]


def draw_slide(c, title, bullets, diagram=None, footer=None, style="std"):
    if style == "std":
        c.setFillColor(BLUE)
        c.rect(0, H - 90, W, 90, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 30)
        c.drawString(40, H - 60, title)
        c.setFillColor(HexColor("#222222"))
        c.setFont("Helvetica", 20)
        y = H - 140
        for b in bullets:
            c.drawString(60, y, "•  " + b)
            y -= 38
        if diagram:
            x = 60
            bw = (W - 120 - 30 * (len(diagram) - 1)) / len(diagram)
            for i, n in enumerate(diagram):
                c.setFillColor(HexColor("#e8f0fb"))
                c.setStrokeColor(BLUE)
                c.roundRect(x, 70, bw, 60, 8, fill=1, stroke=1)
                c.setFillColor(HexColor("#1f2937"))
                c.setFont("Helvetica-Bold", 14)
                c.drawCentredString(x + bw / 2, 95, n)
                if i < len(diagram) - 1:
                    c.line(x + bw, 100, x + bw + 30, 100)
                x += bw + 30
    else:  # re-typed: different template, same words
        c.setFont("Times-Bold", 34)
        c.setFillColor(HexColor("#333333"))
        c.drawCentredString(W / 2, H - 80, title)
        c.setFont("Times-Roman", 22)
        y = H - 160
        for b in bullets:
            c.drawCentredString(W / 2, y, b)
            y -= 40
        if diagram:
            c.setFont("Times-Italic", 16)
            c.drawCentredString(W / 2, 80, " -> ".join(diagram))
    if footer:
        c.setFillColor(HexColor("#6b7280"))
        c.setFont("Helvetica", 9)
        c.drawString(40, 18, footer)


def title_slide(c, title, docno, rev, date, author):
    c.setFillColor(BLUE)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 44)
    c.drawString(60, H / 2 + 20, title)
    c.setFont("Helvetica", 18)
    c.drawString(60, H / 2 - 30, f"Document {docno}   Rev {rev}")
    c.drawString(60, H / 2 - 60, f"{author}  ·  {date}")


def make_deck(path: Path, title: str, docno: str | None, rev: str | None, slides: list, *, style="std",
              date="2026-05-01", author="Alice Andersson", footer_docno=True):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H), invariant=1)
    c.setTitle(title)
    c.setAuthor(author)
    if docno:
        c.setSubject(docno)
        c.setKeywords(f"doc:{docno}; rev:{rev}" if rev else f"doc:{docno}")
    page = 1
    if docno and rev:
        title_slide(c, title, docno, rev, date, author)
        c.showPage()
        page += 1
    for item in slides:
        t, b, *d = item
        foot = f"{docno}    Page {page}" if (docno and footer_docno) else None
        draw_slide(c, t, b, d[0] if d else None, foot, style)
        c.showPage()
        page += 1
    c.save()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def slides_for(keys):
    return [BASE[k] for k in keys]


# ----------------------------------------------------------------------------- pptx
def make_pptx(path: Path, slides: list[tuple[str, list[str]]], title: str, notes: list[str] | None = None):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.core_properties.title = title
    prs.core_properties.author = "Carol Chen"
    img = Image.new("RGB", (400, 200), (31, 95, 168))
    ImageDraw.Draw(img).text((20, 80), "KORA onboarding", fill="white")
    ib = io.BytesIO()
    img.save(ib, "PNG")
    for i, (t, bullets) in enumerate(slides):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = t
        tf = s.placeholders[1].text_frame
        tf.text = bullets[0]
        for b in bullets[1:]:
            tf.add_paragraph().text = b
        if i == 0:
            ib.seek(0)
            s.shapes.add_picture(ib, Inches(6), Inches(5), width=Inches(3))
        if notes and i < len(notes) and notes[i]:
            s.notes_slide.notes_text_frame.text = notes[i]       # like the partner's decks: owner + best-before
    buf = io.BytesIO()
    prs.save(buf)
    # python-pptx writes deterministic zips except timestamps; normalise via re-zip with fixed dates
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, src.read(info.filename))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.getvalue())


# ----------------------------------------------------------------------------- video
def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_recording(pdf: Path, out: Path, vtt_out: Path | None, durations: list[float]):
    doc = pymupdf.open(pdf)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        lines = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=pymupdf.Matrix(1280 / W, 1280 / W), alpha=False)
            name = f"s{i:02d}.png"
            pix.save(tdp / name)
            lines.append(f"file '{name}'\nduration {durations[i]}")
        lines.append(f"file 's{len(doc) - 1:02d}.png'")
        (tdp / "list.txt").write_text("\n".join(lines) + "\n")
        total = sum(durations)
        ff("-f", "concat", "-safe", "1", "-i", str(tdp / "list.txt"),
           "-f", "lavfi", "-i", f"anoisesrc=d={total}:c=pink:a=0.02",
           "-vf", "fps=25,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", "1800k", "-minrate", "1800k", "-maxrate", "1800k", "-bufsize", "3600k",
           "-x264-params", "nal-hrd=cbr", "-c:a", "aac", "-b:a", "96k", "-shortest",
           "-metadata", "title=Architecture review", "-metadata", "creation_time=2026-05-12T09:00:00Z",
           str(out))
    doc.close()


VTT_LINES = [
    # (slide index, speaker, text)
    (0, "Alice Andersson", "Welcome everyone to the architecture review for the regional network."),
    (0, "Alice Andersson", "This is revision A of the network architecture document."),
    (1, "Alice Andersson", "Here is the agenda for today, we will end with decisions."),
    (2, "Alice Andersson", "As you see here, the new edge data centre in Karlskrona sits between aggregation and core."),
    (2, "Bob Berg", "The access rings are still single-homed, which is the main availability risk."),
    (3, "Alice Andersson", "Our design principles: automate everything that is repeated and go dual-homed."),
    (4, "Bob Berg", "The latency budget is five milliseconds end to end, timing comes from PTP grandmasters."),
    (5, "Alice Andersson", "The 5G core user plane moves to the edge, the control plane stays central."),
    (6, "Bob Berg", "We migrate in three phases, starting with lab validation."),
    (7, "Alice Andersson", "Decision: we adopt dual-homed aggregation for all regions."),
    (7, "Alice Andersson", "Action item: Bob validates the lab results by June."),
    (8, "Bob Berg", "The biggest risk is vendor lead time for 100G optics."),
    (8, "Alice Andersson", "Thanks all, the recording and slides will be shared on the site."),
]


def write_vtt(path: Path, durations: list[float]):
    starts = [sum(durations[:i]) for i in range(len(durations))]
    per_slide: dict[int, list] = {}
    for idx, spk, txt in VTT_LINES:
        per_slide.setdefault(idx, []).append((spk, txt))

    def fmt(t):
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    out = ["WEBVTT", ""]
    n = 1
    for idx, lines in per_slide.items():
        span = durations[idx] / len(lines)
        for j, (spk, txt) in enumerate(lines):
            st = starts[idx] + j * span + 0.2
            en = starts[idx] + (j + 1) * span - 0.2
            out += [f"seg-{n}", f"{fmt(st)} --> {fmt(en)}", f"<v {spk}>{txt}</v>", ""]
            n += 1
    path.write_text("\n".join(out))


def make_lab_session(out: Path):
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        font = ImageFont.load_default(size=40) if hasattr(ImageFont, "load_default") else None
        texts = ["Lab bench 3: optics soak test", "Result: 2 of 48 links flap under heat",
                 "Next: replace batch 7 transceivers"]
        lines = []
        for i, t in enumerate(texts):
            im = Image.new("RGB", (1280, 720), (20 + 30 * i, 30, 40))
            ImageDraw.Draw(im).text((80, 320), t, fill="white", font=font)
            im.save(tdp / f"l{i}.png")
            lines.append(f"file 'l{i}.png'\nduration 10")
        lines.append("file 'l2.png'")
        (tdp / "list.txt").write_text("\n".join(lines) + "\n")
        ff("-f", "concat", "-safe", "1", "-i", str(tdp / "list.txt"), "-f", "lavfi", "-i",
           "anoisesrc=d=30:c=brown:a=0.02", "-vf", "fps=25,format=yuv420p", "-c:v", "libx264",
           "-preset", "veryfast", "-b:v", "1200k", "-c:a", "aac", "-b:a", "64k", "-shortest", str(out))


# ----------------------------------------------------------------------------- main
def main(root: Path):
    if root.exists():
        shutil.rmtree(root)
    sp = root / "sharepoint" / "network-team"
    od = root / "onedrive" / "bob"
    play = root / "play"

    # Network Architecture revisions
    dates = {"PA1": "2026-03-02", "PA2": "2026-03-20", "A": "2026-04-15", "B": "2026-09-01"}
    for rev, keys in REVISIONS.items():
        make_deck(sp / "Presentations" / f"Network_Architecture_{rev}.pdf", "Network Architecture", DOC_NA, rev,
                  slides_for(keys), date=dates[rev])
    rev_a = (sp / "Presentations" / "Network_Architecture_A.pdf").read_bytes()

    # 5G core overview: shares 6 slide bodies with rev A but carries its own doc number in the footer
    five = []
    for k, custom in FIVE_G:
        if k == "title5g":
            continue
        five.append(custom if custom else BASE[k])
    make_deck(sp / "Presentations" / "5G_Core_Overview.pdf", "5G Core Overview", DOC_5G, "A", five,
              date="2026-06-10", author="Carol Chen")

    # excerpt: pages 4-6 of rev A, copied verbatim (keeps rev A footers)
    d = pymupdf.open(stream=rev_a, filetype="pdf")
    ex = pymupdf.open()
    ex.insert_pdf(d, from_page=3, to_page=5)
    (sp / "Presentations" / "Network_Architecture_excerpt.pdf").write_bytes(ex.tobytes(garbage=4, deflate=True, no_new_id=True))

    # filename-versioned team updates (no document control)
    tu = [("Status", ["Lab validation 60% complete", "Two open defects"]),
          ("Staffing", ["Hiring one transport engineer"]),
          ("Next steps", ["Finish lab validation", "Prepare pilot"])]
    make_deck(sp / "Team" / "Team_Update.pdf", "Team Update", None, None, tu, author="Bob Berg")
    tu2 = [tu[0], ("Staffing", ["Hiring one transport engineer", "Summer cover agreed"]), tu[2]]
    make_deck(sp / "Team" / "Team_Update_Final.pdf", "Team Update", None, None, tu2, author="Bob Berg")
    tu3 = tu2 + [("Budget", ["Optics budget approved", "Travel freeze lifted"])]
    make_deck(sp / "Team" / "Team_Update_Final_v2.pdf", "Team Update", None, None, tu3, author="Bob Berg")

    # PPTX onboarding deck + copies + v2
    onb = [("Welcome to the network team", ["Who we are", "How we work"]),
           ("Tools", ["Ticketing", "Lab booking", "Wiki"]),
           ("Your first week", ["Meet your buddy", "Lab safety training"]),
           ("Contacts", ["Alice Andersson - team lead", "Carol Chen - core"])]
    old_notes = [f"{t}\nSlide responsible: Bob Berg\tBest Before: April 2020\n\nTalk track: keep it short."
                 for t, _ in onb]
    make_pptx(sp / "Team" / "Onboarding.pptx", onb, "Onboarding", notes=old_notes)
    onb2 = onb[:2] + [("Your first week", ["Meet your buddy", "Lab safety training", "Shadow an on-call shift"])] + onb[3:] \
        + [("FAQ", ["Where is the lab?", "How do I get VPN access?"])]
    od.mkdir(parents=True, exist_ok=True)
    make_pptx(od / "Projects" / "Onboarding_v2.pptx", onb2, "Onboarding",
              notes=[f"{t}\nSlide responsible: Carol Chen\tBest Before: December 2030" for t, _ in onb2])
    for name in ("Onboarding.pptx", "Onboarding (1).pptx"):
        shutil.copyfile(sp / "Team" / "Onboarding.pptx", od / "Projects" / name)

    # Bob's OneDrive: download copies, re-save, unique notes, re-typed deck
    (od / "Downloads").mkdir(parents=True, exist_ok=True)
    (od / "Downloads" / "Network_Architecture_A (1).pdf").write_bytes(rev_a)
    (od / "Downloads" / "Network_Architecture_A (2).pdf").write_bytes(rev_a)
    shutil.copyfile(sp / "Presentations" / "5G_Core_Overview.pdf", od / "Downloads" / "5G_Core_Overview (1).pdf")
    import pikepdf
    (od / "Desktop").mkdir(parents=True, exist_ok=True)
    with pikepdf.open(io.BytesIO(rev_a)) as p:
        p.docinfo["/Producer"] = "Microsoft: Print To PDF"
        p.docinfo["/ModDate"] = "D:20260502120000Z"
        p.save(od / "Desktop" / "Network_Architecture_A_resaved.pdf", linearize=True)
    make_deck(od / "Notes" / "Bob_Lab_Findings.pdf", "Lab findings", None, None,
              [("Optics soak test", ["2 of 48 links flap above 45C", "Batch 7 transceivers suspected"]),
               ("Recommendation", ["Replace batch 7 before pilot", "Re-run soak test in August"])],
              author="Bob Berg")
    make_deck(od / "Drafts" / "Architecture_notes_retyped.pdf", "Network Architecture", None, None,
              slides_for(REVISIONS["A"]), style="retyped", author="Bob Berg")

    # recordings
    durations = [9, 8, 12, 9, 9, 9, 8, 12, 10]           # title + 8 slides of rev A
    (sp / "Recordings").mkdir(parents=True, exist_ok=True)
    rec = sp / "Recordings" / "Architecture_Review_2026-05-12.mp4"
    make_recording(sp / "Presentations" / "Network_Architecture_A.pdf", rec, None, durations)
    write_vtt(rec.with_suffix(".vtt"), durations)
    play.mkdir(parents=True, exist_ok=True)
    ff("-i", str(rec), "-vf", "scale=854:480", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
       "-c:a", "aac", "-b:a", "64k", str(play / "Architecture_Review_2026-05-12_480p.mp4"))
    (od / "Recordings").mkdir(parents=True, exist_ok=True)
    make_lab_session(od / "Recordings" / "Bob_Lab_Session.mp4")

    # ------------------------------------------------------------------ repository metadata
    (sp / "_kora_source.json").write_text(json.dumps({
        "default_owner": "alice", "default_acl": ["group:network-team"], "default_retention_label": "standard",
        "items": {
            "Presentations/Network_Architecture_PA1.pdf": {"created": "2026-03-02T09:00:00", "modified": "2026-03-02T09:00:00"},
            "Presentations/Network_Architecture_PA2.pdf": {"created": "2026-03-20T09:00:00", "modified": "2026-03-20T09:00:00"},
            "Presentations/Network_Architecture_A.pdf": {"created": "2026-04-15T09:00:00", "modified": "2026-04-15T09:00:00", "retention_label": "record"},
            "Presentations/Network_Architecture_B.pdf": {"created": "2026-09-01T09:00:00", "modified": "2026-09-01T09:00:00", "retention_label": "record"},
            "Presentations/5G_Core_Overview.pdf": {"owner": "carol", "created": "2026-06-10T09:00:00", "modified": "2026-06-10T09:00:00"},
            "Presentations/Network_Architecture_excerpt.pdf": {"owner": "carol", "created": "2026-06-11T09:00:00", "modified": "2026-06-11T09:00:00"},
            "Team/Team_Update.pdf": {"owner": "bob", "created": "2026-02-01T09:00:00", "modified": "2026-02-01T09:00:00"},
            "Team/Team_Update_Final.pdf": {"owner": "bob", "created": "2026-02-03T09:00:00", "modified": "2026-02-03T09:00:00"},
            "Team/Team_Update_Final_v2.pdf": {"owner": "bob", "created": "2026-02-04T09:00:00", "modified": "2026-02-04T09:00:00"},
            "Team/Onboarding.pptx": {"owner": "carol", "created": "2026-01-10T09:00:00", "modified": "2026-01-10T09:00:00"},
            "Recordings/Architecture_Review_2026-05-12.mp4": {"created": "2026-05-12T11:00:00", "modified": "2026-05-12T11:00:00"},
            "Recordings/Architecture_Review_2026-05-12.vtt": {"created": "2026-05-12T11:00:00", "modified": "2026-05-12T11:00:00"},
        }}, indent=2))
    (od / "_kora_source.json").write_text(json.dumps({
        "default_owner": "bob", "default_acl": ["user:bob"], "default_retention_label": "standard",
        "items": {
            "Downloads/Network_Architecture_A (1).pdf": {"created": "2026-04-20T10:00:00"},
            "Downloads/Network_Architecture_A (2).pdf": {"created": "2026-04-21T10:00:00", "legal_hold": True},
            "Downloads/5G_Core_Overview (1).pdf": {"created": "2026-06-12T10:00:00", "acl": ["user:bob", "user:frank"]},
            "Desktop/Network_Architecture_A_resaved.pdf": {"created": "2026-05-02T12:00:00"},
            "Projects/Onboarding.pptx": {"created": "2026-01-15T10:00:00"},
            "Projects/Onboarding (1).pptx": {"created": "2026-01-16T10:00:00"},
            "Projects/Onboarding_v2.pptx": {"created": "2026-02-15T10:00:00", "modified": "2026-02-15T10:00:00"},
        }}, indent=2))
    (play / "_kora_source.json").write_text(json.dumps({
        "default_owner": "alice", "default_acl": ["group:network-team"], "items": {
            "Architecture_Review_2026-05-12_480p.mp4": {"created": "2026-05-13T08:00:00"}}}, indent=2))

    (root / "principals.json").write_text(json.dumps([
        {"id": "alice", "display_name": "Alice Andersson", "groups": ["network-team"], "manager_id": "erik", "team": "network"},
        {"id": "bob", "display_name": "Bob Berg", "groups": ["network-team"], "manager_id": "alice", "team": "network", "status": "departing"},
        {"id": "carol", "display_name": "Carol Chen", "groups": ["network-team", "core-team"], "manager_id": "alice", "team": "network"},
        {"id": "dave", "display_name": "Dave Dahl", "groups": ["governance"], "team": "governance"},
        {"id": "erin", "display_name": "Erin Ek", "groups": ["governance"], "team": "it-operations"},
        {"id": "frank", "display_name": "Frank Fors (partner)", "groups": []},
        {"id": "gina", "display_name": "Gina Gustafsson", "groups": ["governance"], "team": "audit"},
        {"id": "erik", "display_name": "Erik Eriksson", "groups": ["leadership"]},
    ], indent=2))
    (root / "sources.json").write_text(json.dumps([
        {"name": "sp-network-team", "system": "sharepoint", "root": "sharepoint/network-team", "site": "network-team"},
        {"name": "od-bob", "system": "onedrive", "root": "onedrive/bob", "site": "bob"},
        {"name": "play", "system": "ericsson_play", "root": "play", "site": "play"},
    ], indent=2))
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    print(f"samples written to {root} ({total / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "samples").resolve())
