"""
Google Drive ingestion: format detection, signature checks, and one extractor per format.
Pure in-memory: no network, no database, no Firestore.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.core.config import settings
from app.services import drive_formats as F
from tests import test_drive_ingest_helpers as fx  # noqa: E402  (tests/ is on sys.path under pytest)


def _run(data: bytes, fmt: str, ocr=None) -> F.ExtractionResult:
    return F.extract_bytes(data, fmt, deadline=F.Deadline(60), ocr=ocr)


# ---- detection -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,mime,expected,supported", [
    ("a.pdf", "application/pdf", "pdf", True),
    ("a.docx", F.MIME_DOCX, "docx", True),
    ("a.doc", "application/msword", "doc", False),
    ("Doc", F.GOOGLE_DOC, "google_doc", True),
    ("a.xlsx", F.MIME_XLSX, "xlsx", True),
    ("a.xls", "application/vnd.ms-excel", "xls", True),
    ("a.csv", "text/csv", "csv", True),
    ("Sheet", F.GOOGLE_SHEET, "google_sheet", True),
    ("a.pptx", F.MIME_PPTX, "pptx", True),
    ("a.ppt", "application/vnd.ms-powerpoint", "ppt", False),
    ("Deck", F.GOOGLE_SLIDES, "google_slides", True),
    ("a.txt", "text/plain", "txt", True),
    ("a.md", "text/plain", "md", True),           # Drive reports .md as text/plain; extension refines it
    ("a.rtf", "application/rtf", "rtf", True),
    ("a.html", "text/html", "html", True),
    ("a.htm", "application/octet-stream", "html", True),
    ("a.json", "application/json", "json", True),
    ("a.xml", "text/xml", "xml", True),
    ("a.jpg", "image/jpeg", "image", True),
    ("a.webp", "image/webp", "image", True),
    ("a.tiff", "image/tiff", "image", True),
    ("a.zip", "application/zip", "zip", True),
    ("a.docm", "application/vnd.ms-word.document.macroEnabled.12", "docm", False),
    ("a.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", "xlsm", False),
    ("setup.exe", "application/x-msdownload", "unsupported", False),
    ("run.sh", "text/plain", "unsupported", False),
    ("Form", "application/vnd.google-apps.form", "unsupported", False),
    ("Link", F.GOOGLE_SHORTCUT, "unsupported", False),
    ("video.mp4", "video/mp4", "unsupported", False),
])
def test_classify_by_mime_and_extension(name, mime, expected, supported):
    info = F.classify(name, mime)
    assert info.format == expected
    assert info.supported is supported
    if not supported:
        assert info.reason, "unsupported files must carry a reason"


def test_google_workspace_files_are_exported_to_office_formats():
    assert F.classify("x", F.GOOGLE_DOC).export_mime == F.MIME_DOCX
    assert F.classify("x", F.GOOGLE_SHEET).export_mime == F.MIME_XLSX   # every sheet, unlike CSV export
    assert F.classify("x", F.GOOGLE_SLIDES).export_mime == F.MIME_PPTX


def test_signatures_reject_executables_mismatches_and_encrypted_office():
    assert _run(b"MZ\x90\x00" + b"\x00" * 100, "pdf").status == F.ST_UNSUPPORTED
    assert _run(b"\x7fELF" + b"\x00" * 100, "txt").status == F.ST_UNSUPPORTED
    enc = _run(fx.ooxml_encrypted_stub(), "docx")
    assert enc.status == F.ST_ENCRYPTED and "password" in enc.error
    assert _run(b"", "pdf").status == F.ST_EMPTY
    bad = _run(b"\x00\x01\x02 random binary \xff\xfe" * 20, "json")
    assert bad.status == F.ST_ERROR and "does not match" in bad.error


def test_pdf_named_docx_is_parsed_as_what_it_really_is():
    res = _run(fx.make_text_pdf(["Property Name: Disguised Villa"]), "docx")
    assert res.format == "pdf" and res.usable
    assert any("parsed as pdf" in w for w in res.warnings)


def test_filenames_are_sanitised():
    assert F.safe_filename("../../etc/passwd") == "passwd"
    assert F.safe_filename("C:\\x\\evil\x00name.pdf") == "evilname.pdf"
    assert F.safe_filename("") == "unnamed"


# ---- PDF -------------------------------------------------------------------------------------------------------------

def test_pdf_multi_page_text_keeps_page_numbers():
    res = _run(fx.make_text_pdf(["Property Name: Page One Villa", "Price: 12,000,000 THB"]), "pdf")
    assert res.status == F.ST_OK
    assert [s.ref["page"] for s in res.segments] == [1, 2]
    assert res.meta["pages"] == 2


def test_scanned_pdf_needs_ocr_when_ocr_is_off():
    res = _run(fx.make_scanned_pdf(), "pdf")
    assert res.status == F.ST_NEEDS_OCR
    assert "OCR is not enabled" in res.error
    assert res.images, "embedded page image is reported"


def test_scanned_pdf_is_read_with_ocr():
    calls = []

    def fake_ocr(data, purpose):
        calls.append((len(data), purpose))
        return "Property Name: Scanned Villa\nPrice: 9,900,000 THB"

    res = _run(fx.make_scanned_pdf(pages=2), "pdf", ocr=fake_ocr)
    assert res.status == F.ST_OK
    assert len(calls) == 2 and all(p == "page" for _, p in calls)
    assert all(s.ref.get("ocr") for s in res.segments)
    assert res.meta["ocr_pages"] == 2


def test_pdf_ocr_page_budget(monkeypatch):
    monkeypatch.setattr(settings, "DRIVE_INGEST_OCR_MAX_PAGES", 1, raising=False)
    res = _run(fx.make_scanned_pdf(pages=3), "pdf", ocr=lambda d, p: "text")
    assert res.meta["ocr_pages"] == 1 and res.status == F.ST_PARTIAL


def test_pdf_with_some_image_only_pages_is_partial():
    res = _run(fx.make_mixed_pdf(), "pdf")
    assert res.status == F.ST_PARTIAL
    assert res.meta["image_only_pages"] == [2]


def test_encrypted_and_corrupted_pdfs():
    assert _run(fx.make_encrypted_pdf(), "pdf").status == F.ST_ENCRYPTED
    truncated = fx.make_text_pdf(["x"])[:60]
    assert _run(truncated, "pdf").status in (F.ST_ERROR, F.ST_EMPTY)


# ---- Word / Google Docs ----------------------------------------------------------------------------------------------

def test_docx_paragraphs_headings_tables_header_footer():
    data = fx.make_docx(
        ["Luxury villa with ocean views.", "Price: 32,000,000 THB"],
        heading="Surin Cliff Villa",
        kv_table=[["Bedrooms", "5"], ["Bathrooms", "6"]],
        table=[["Unit", "Size", "Price"], ["A1", "220 sqm", "12,000,000"], ["A2", "260 sqm", "14,500,000"]],
        header_text="Varsovia Estate — confidential",
        footer_text="Page footer text",
    )
    res = _run(data, "docx")
    assert res.status == F.ST_OK
    kinds = {s.kind for s in res.segments}
    assert {"heading", "paragraph", "table_row", "header", "footer"} <= kinds
    para = next(s for s in res.segments if "ocean views" in s.text)
    assert para.ref.get("section") == "Surin Cliff Villa"
    assert any(r["data"].get("Unit") == "A2" and r["ref"]["table"] == 2 for r in res.records)


def test_google_doc_export_keeps_its_format_label():
    res = _run(fx.make_docx(["Hello"]), "google_doc")
    assert res.format == "google_doc" and res.usable


def test_corrupted_docx_is_an_error_not_a_crash():
    broken = fx.make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<not-xml"})
    res = _run(broken, "docx")
    assert res.status == F.ST_ERROR


# ---- Spreadsheets ----------------------------------------------------------------------------------------------------

def test_xlsx_every_sheet_with_header_detection_and_row_refs():
    data = fx.make_xlsx({"Intro": [["Portfolio 2026"], [], ["generated by sales"]],
                         "Listings": fx.listing_rows(), "Prices": [["Name", "Price"], ["Kamala Sky Villa", 46000000]]})
    res = _run(data, "xlsx")
    assert res.status == F.ST_OK
    listing = [r for r in res.records if r["ref"]["sheet"] == "Listings"]
    assert len(listing) == 2                       # the blank row is skipped
    assert listing[0]["ref"]["row"] == 2           # 1-based sheet row
    assert listing[0]["data"]["Property Name"] == "Kamala Sky Villa"
    assert {s["name"] for s in res.meta["sheets"]} == {"Intro", "Listings", "Prices"}


def test_xlsx_row_limit_marks_partial(monkeypatch):
    monkeypatch.setattr(settings, "DRIVE_INGEST_MAX_ROWS", 5, raising=False)
    rows = [["Name", "Price"]] + [[f"Villa {i}", 1000 + i] for i in range(50)]
    res = _run(fx.make_xlsx({"S": rows}), "xlsx")
    assert res.status == F.ST_PARTIAL and len(res.records) <= 5


def test_xls_garbage_is_an_error():
    res = _run(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, "xls")
    assert res.status in (F.ST_ERROR, F.ST_ENCRYPTED)


def test_csv_semicolon_delimited():
    data = "Name;Price;Bedrooms\nRawai Pool Villa;12 500 000;3\n".encode("utf-8")
    res = _run(data, "csv")
    assert res.records[0]["data"] == {"Name": "Rawai Pool Villa", "Price": "12 500 000", "Bedrooms": "3"}
    assert res.records[0]["ref"] == {"sheet": "csv", "row": 2}


def test_google_sheet_export_label():
    res = _run(fx.make_xlsx({"Main": [["Name", "Price"], ["X Villa", 1]]}), "google_sheet")
    assert res.format == "google_sheet" and res.records


# ---- PowerPoint / Google Slides --------------------------------------------------------------------------------------

def test_pptx_slides_titles_tables_notes_and_pictures():
    data = fx.make_pptx([
        {"title": "Layan Hills Estate", "text": "Ready to move in\nPrice: from 28M THB", "notes": "Speaker: mention ROI"},
        {"title": "Unit types", "table": [["Type", "Bedrooms", "Price"], ["Villa A", "3", "28,000,000"]], "picture": True},
    ])
    res = _run(data, "pptx")
    assert res.status == F.ST_OK and res.meta["slides"] == 2
    assert any(s.kind == "heading" and s.ref["slide"] == 1 for s in res.segments)
    assert any(s.kind == "notes" and s.ref["slide"] == 1 for s in res.segments)
    assert res.records[0]["ref"]["slide"] == 2 and res.records[0]["data"]["Type"] == "Villa A"
    assert any(i["ref"].get("slide") == 2 for i in res.images)


def test_google_slides_export_label():
    assert _run(fx.make_pptx([{"title": "Deck"}]), "google_slides").format == "google_slides"


# ---- Text, Markdown, RTF, HTML ---------------------------------------------------------------------------------------

def test_text_markdown_and_rtf():
    txt = _run(b"Property Name: Plain Villa\n\nPrice: 5,000,000 THB", "txt")
    assert len(txt.segments) == 2 and txt.segments[1].ref == {"block": 2}
    md = _run(b"# Nai Harn Villas\n\n| Name | Price |\n|---|---|\n| Villa 1 | 9,000,000 |\n", "md")
    assert md.segments[0].kind == "heading"
    assert md.records and md.records[0]["data"] == {"Name": "Villa 1", "Price": "9,000,000"}
    rtf = _run(fx.make_rtf("Property Name: RTF Villa\nPrice: 7,000,000 THB"), "rtf")
    assert rtf.status == F.ST_OK and "RTF Villa" in rtf.text() and "Arial" not in rtf.text()


def test_html_ignores_scripts_keeps_tables_and_jsonld():
    html = b"""<html><head><title>Kata View</title><meta name="description" content="Sea view condo">
    <script>alert('x'); var secret = 'IGNORE ME';</script>
    <script type="application/ld+json">{"@type":"Residence","name":"Kata View","price":"6500000"}</script></head>
    <body><h1>Kata View Condo</h1><p>Two bedrooms near the beach.</p>
    <table><tr><th>Unit</th><th>Price</th></tr><tr><td>A</td><td>6,500,000</td></tr></table></body></html>"""
    res = _run(html, "html")
    text = res.text()
    assert "IGNORE ME" not in text and "alert" not in text
    assert "Two bedrooms near the beach." in text
    assert any(r["ref"].get("table") == 1 and r["data"]["Unit"] == "A" for r in res.records)
    assert any(r["ref"].get("json_ld") == 1 and r["data"]["name"] == "Kata View" for r in res.records)


# ---- JSON / XML ------------------------------------------------------------------------------------------------------

def test_json_list_under_known_key_with_paths():
    res = _run(fx.json_listings(), "json")
    assert len(res.records) == 2
    assert res.records[0]["ref"] == {"json_path": "$.properties[0]"}
    assert res.records[0]["data"]["address.addressRegion"] == "Phuket"


def test_json_malformed_and_too_deep():
    assert _run(b'{"a": [1, 2', "json").status == F.ST_ERROR
    assert _run(("[" * 100 + "]" * 100).encode(), "json").status == F.ST_ERROR


def test_xml_repeated_elements_become_records():
    res = _run(fx.xml_listings(), "xml")
    assert len(res.records) == 2
    first = res.records[0]
    assert first["ref"]["xml_path"] == "/feed/listings/property[1]"
    assert first["data"]["@id"] == "rawai-1" and first["data"]["name"] == "Rawai Hill Villa"


def test_xml_entity_expansion_and_external_entities_are_refused():
    bomb = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
    <root>&lol2;</root>"""
    xxe = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>"""
    for doc in (bomb, xxe):
        res = _run(doc, "xml")
        assert res.status == F.ST_ERROR and "refused" in res.error


# ---- Images ----------------------------------------------------------------------------------------------------------

def test_image_needs_ocr_when_off_and_reads_text_with_ocr():
    png = fx.make_png()
    off = _run(png, "image")
    assert off.status == F.ST_NEEDS_OCR
    on = _run(png, "image", ocr=lambda d, p: "Price 9,900,000 THB")
    assert on.status == F.ST_OK and on.segments[0].ref == {"image": 1, "ocr": True}


def test_image_pixel_limit_and_corruption(monkeypatch):
    monkeypatch.setattr(settings, "DRIVE_INGEST_MAX_IMAGE_PIXELS", 1000, raising=False)
    assert _run(fx.make_png((200, 80)), "image").status == F.ST_TOO_LARGE
    monkeypatch.setattr(settings, "DRIVE_INGEST_MAX_IMAGE_PIXELS", 60_000_000, raising=False)
    assert _run(fx.make_png()[:40], "image").status == F.ST_ERROR


# ---- ZIP -------------------------------------------------------------------------------------------------------------

def test_zip_mixed_members_with_member_refs():
    data = fx.make_zip({
        "listings.csv": b"Name,Price\nZip Villa,1000000\n",
        "brochure.pdf": fx.make_text_pdf(["Property Name: Zip PDF Villa"]),
        "docs/readme.txt": b"Property Name: Zip Text Villa",
        "old.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100,
        "__MACOSX/._junk": b"x",
    })
    res = _run(data, "zip")
    assert res.status == F.ST_PARTIAL          # old.doc is unsupported
    names = {n: c.status for n, c in res.children}
    assert names["listings.csv"] == F.ST_OK and names["brochure.pdf"] == F.ST_OK and names["old.doc"] == F.ST_UNSUPPORTED
    assert "._junk" not in names
    assert res.records[0]["ref"]["archive_member"] == "listings.csv"


def test_zip_bomb_ratio_total_size_and_member_count(monkeypatch):
    zeros = fx.make_zip({"zeros.txt": b"0" * 5_000_000})
    res = _run(zeros, "zip")
    assert res.children[0][1].status == F.ST_TOO_LARGE and "ratio" in res.children[0][1].error
    monkeypatch.setattr(settings, "DRIVE_INGEST_ZIP_MAX_TOTAL_BYTES", 1000, raising=False)
    big = fx.make_zip({"a.txt": b"a" * 2000}, compression=zipfile.ZIP_STORED)
    assert _run(big, "zip").status == F.ST_TOO_LARGE
    monkeypatch.setattr(settings, "DRIVE_INGEST_ZIP_MAX_TOTAL_BYTES", 100 * 1024 * 1024, raising=False)
    monkeypatch.setattr(settings, "DRIVE_INGEST_ZIP_MAX_MEMBERS", 3, raising=False)
    many = fx.make_zip({f"{i}.txt": b"x" for i in range(5)})
    assert _run(many, "zip").status == F.ST_TOO_LARGE


def test_zip_path_traversal_nested_and_encrypted_members():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", b"Property Name: Evil")
        zf.writestr("inner.zip", fx.make_zip({"a.txt": b"hello"}))
        zf.writestr("locked.txt", b"secret")
    raw = bytearray(buf.getvalue())
    # zipfile cannot write encrypted entries; set the "encrypted" flag bit in the central directory record instead
    cd = raw.find(b"PK\x01\x02", raw.find(b"PK\x01\x02locked") if b"PK\x01\x02locked" in raw else 0)
    while cd != -1:
        name_len = int.from_bytes(raw[cd + 28:cd + 30], "little")
        if bytes(raw[cd + 46:cd + 46 + name_len]) == b"locked.txt":
            raw[cd + 8] |= 0x1
            break
        cd = raw.find(b"PK\x01\x02", cd + 4)
    res = _run(bytes(raw), "zip")
    status = {n: (c.status, c.error) for n, c in res.children}
    assert status["evil.txt"][0] == F.ST_UNSUPPORTED and "Unsafe path" in status["evil.txt"][1]
    assert status["locked.txt"][0] == F.ST_ENCRYPTED
    # one level of nesting is opened (the dispatcher passes depth+1); deeper levels are refused
    inner = dict(res.children)["inner.zip"]
    assert inner.format == "zip"
    deeper = F.extract_bytes(fx.make_zip({"x.zip": fx.make_zip({"y.txt": b"y"})}), "zip", zip_depth=1)
    assert dict(deeper.children)["x.zip"].status == F.ST_UNSUPPORTED


def test_size_limit_before_parsing(monkeypatch):
    monkeypatch.setattr(settings, "DRIVE_INGEST_MAX_FILE_BYTES", 100, raising=False)
    assert _run(b"x" * 200, "txt").status == F.ST_TOO_LARGE


def test_deadline_stops_long_parsing():
    rows = [["Name", "Price"]] + [[f"V{i}", i] for i in range(3000)]
    with pytest.raises(F.ExtractionTimeout):
        F.extract_bytes(fx.make_xlsx({"S": rows}), "xlsx", deadline=F.Deadline(-1))
