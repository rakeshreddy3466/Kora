"""Pure unit tests: versioning primitives, fingerprints, file-type guards."""
from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image, ImageDraw

from kora.fingerprint import minhash as mh
from kora.fingerprint.phash import hamming, phash
from kora.fingerprint.text import shingles
from kora.security.filetype import UnsafeFile, check_zip, extension_mismatch, sniff
from kora.versioning import docnumber, naming, revision
from kora.versioning.diff import UnitView, diff


def test_ericsson_revision_order():
    labels = ["B", "PA1", "A", "PB1", "PA2", "PB2", "C", "AA", "PA10"]
    assert sorted(labels, key=lambda x: revision.parse(x).key) == ["PA1", "PA2", "PA10", "A", "PB1", "PB2", "B", "C", "AA"]
    assert revision.parse("A").released and not revision.parse("PA1").released
    assert sorted(["R2A", "R1B", "P1A", "R1A"], key=lambda x: revision.parse(x).key) == ["P1A", "R1A", "R1B", "R2A"]
    assert revision.parse("Rev. B").label == "B"
    assert revision.parse("v1.10").key > revision.parse("v1.9").key
    with pytest.raises(TypeError):
        _ = revision.parse("A") < revision.parse("R1A")


def test_filename_versions_and_copies():
    a = naming.parse("Network_Architecture_A (1).pdf")
    assert (a.stem_key, a.copy_marker, a.tokens) == ("network architecture", 1, ["A"])
    assert naming.parse("Copy of Team Update - Copy.pdf").stem_key == "team update"
    keys = [revision.from_filename_tokens(naming.parse(n).tokens).key
            for n in ("Team_Update_draft.pdf", "Team_Update.pdf", "Team_Update_Final.pdf", "Team_Update_Final_v2.pdf")]
    assert keys == sorted(keys)
    r = naming.parse("Architecture_Review_2026-05-12_480p.mp4")
    assert r.stem_key == "architecture review" and r.date_token == "2026-05-12"


def test_docnumber_detection_prefers_metadata():
    d = docnumber.detect({"keywords": "doc:1/1551-LZA 701 0001 Uen; rev:PA2"}, "Rev B", "")
    assert d["doc_number"] == "1/1551-LZA 701 0001 UEN" and d["revision"] == "PA2" and d["revision_source"] == "doc_metadata"
    d = docnumber.detect({}, "Document EN/LZT 123 4567   Rev R1A", "")
    assert d["doc_number"] == "EN/LZT 123 4567" and d["revision"] == "R1A"
    assert docnumber.detect({}, "nothing here", "") == {}


def _u(i, body, unit=None, text=""):
    return UnitView(i, unit or body + str(i), body, text or body)


def test_slide_diff_classifies_every_change():
    old = [_u(1, "t"), _u(2, "agenda"), _u(3, "x", text="Transport layer\nLattency budget 5 ms"),
           _u(4, "core"), _u(5, "mig")]
    new = [_u(1, "t", unit="t-renumbered"), _u(2, "y", text="Transport layer\nLatency budget 5 ms"),
           _u(3, "mig"), _u(4, "core"), _u(5, "risks", text="Risks\nVendor lead times")]
    d = diff(old, new)
    assert d["unchanged"] + d["unchanged_body"] >= 1
    assert d["unchanged_body"] >= 1                       # page renumbered / footer changed
    assert [m["title"] for m in d["modified"]] == ["Transport layer"]
    assert any(r["title"] == "agenda" for r in d["removed"])
    assert any(a["title"] == "Risks" for a in d["added"])
    assert d["moved"], "core/mig swapped order"


def test_minhash_estimates_jaccard():
    a = shingles(" ".join(f"word{i}" for i in range(300)))
    b = shingles(" ".join(f"word{i}" for i in range(30, 330)))
    true_j = len(a & b) / len(a | b)
    est = mh.jaccard(mh.signature(a), mh.signature(b))
    assert abs(est - true_j) < 0.12
    sa = mh.signature(a)
    assert mh.jaccard(sa, mh.from_bytes(mh.to_bytes(sa))) == 1.0


def test_phash_survives_rescale_and_recompression():
    im = Image.new("RGB", (960, 540), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 960, 90], fill=(31, 95, 168))
    d.rectangle([60, 300, 400, 380], outline="black", width=4)
    small = im.resize((427, 240))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=40)
    other = Image.new("RGB", (960, 540), (31, 95, 168))
    assert hamming(phash(im), phash(Image.open(buf))) <= 6
    assert hamming(phash(im), phash(other)) > 12


def test_sniff_ignores_extension_and_flags_mismatch():
    assert sniff(b"%PDF-1.7 ...", "x.pptx") == "pdf"
    assert extension_mismatch("pdf", "evil.pptx")
    assert sniff(b"\x00\x00\x00\x18ftypmp42", "rec.mp4") == "video"
    assert sniff(b"WEBVTT\n\n", "t.vtt") == "transcript"


def test_zip_guard_rejects_bombs_and_traversal(tmp_path):
    bomb = tmp_path / "b.pptx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "presentationml")
        z.writestr("ppt/big.bin", b"\0" * (30 * 1024 * 1024))
    with pytest.raises(UnsafeFile):
        check_zip(bomb, max_uncompressed=10 * 1024 * 1024)
    trav = tmp_path / "t.pptx"
    with zipfile.ZipFile(trav, "w") as z:
        z.writestr("[Content_Types].xml", "presentationml")
        z.writestr("../../etc/x", "x")
    with pytest.raises(UnsafeFile):
        check_zip(trav, max_uncompressed=10 ** 9)


def test_ericsson_custom_properties_beat_file_names(tmp_path):
    """Real-data finding: 'final' was Rev C and 'final2' Rev B. Document control must win."""
    import zipfile
    from kora.extraction.office import custom_props, ericsson_meta
    from kora.versioning import docnumber
    p = tmp_path / "deck_final2.pptx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("docProps/custom.xml",
                   '<?xml version="1.0"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
                   'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                   '<property fmtid="{x}" pid="2" name="DocNo"><vt:lpwstr>1/221 09-ABC 123 4567 Uen</vt:lpwstr></property>'
                   '<property fmtid="{x}" pid="3" name="Revision"><vt:lpwstr>B</vt:lpwstr></property>'
                   '<property fmtid="{x}" pid="4" name="Prepared"><vt:lpwstr>ETESTAB Test Author</vt:lpwstr></property>'
                   '</Properties>')
    meta = ericsson_meta(custom_props(p))
    d = docnumber.detect(meta, "", "")
    assert d == {"doc_number": "1/221 09-ABC 123 4567 UEN", "doc_number_source": "doc_metadata",
                 "revision": "B", "revision_source": "doc_metadata"}
    footer = "ETESTAB Test Author  |  1/221 09-ABC 123 4567 Uen  |  C  |  2019-10-11  |  Open"
    assert (docnumber.find_docno(footer), docnumber.find_revision(footer)) == ("1/221 09-ABC 123 4567 UEN", "C")


def test_erasure_name_matching_is_accent_insensitive():
    """Real-data finding: a surname spelled without an accent in the chat and with one on the slide."""
    from kora.privacy.dsar import name_pattern
    p = name_pattern("Jose Nunez")
    assert p.search("Hack / José Núñez / 2023") and p.search("JOSE NUNEZ: hi")
    assert not p.search("Jose Nuno")


def test_additive_migration_adds_new_columns(tmp_path):
    import sqlite3
    from kora import db
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("create table slide_blobs (unit_hash varchar primary key, blob_key varchar, size_bytes integer, "
                "fmt varchar, first_content_sha varchar, created_at datetime)")
    con.commit()
    con.close()
    db.init_engine(f"sqlite:///{path}")
    db.create_all()
    cols = {r[1] for r in sqlite3.connect(path).execute("pragma table_info(slide_blobs)")}
    assert "page_index" in cols
