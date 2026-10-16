"""Known gaps from live testing: diagram labels stored as image tiles, composition groups, interfaces named in prose,
the "no ports extracted" answer, the closest-passages block, the Documents scope line and cross-project isolation."""
import io
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from app import controls, diagram, extract, ocr, qa, retrieve

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 1. labels stored as image tiles
def test_verified_label_rejects_ocr_misreads_and_joins_split_words():
    vocab = {"ReqRainSensing", "MirrorAdjust", "StaProfilePerson", "AccessKeyless"}
    assert diagram._verified_label("RegRainSensing", vocab) is None          # 'Reg' misread of 'Req'
    assert diagram._verified_label("Mi rrorAdjust", vocab) == "MirrorAdjust"  # stray inner space
    assert diagram._verified_label("StaProfilePerson / status", vocab) == "StaProfilePerson / status"
    assert diagram._verified_label("[ManAdjustment]AccessKeyless", vocab) == "[ManAdjustment]AccessKeyless"
    assert diagram._verified_label("AccessRemoteKey|!", vocab) is None
    assert diagram._verified_label("Ab", vocab) is None


def _tile(page, rect: pymupdf.Rect, text: str, parts: int = 2) -> None:
    """Draw `text` as `parts` side-by-side high-resolution image tiles (no text layer), like the real PDFs."""
    scale = 8  # px per pt, far above TILE_MIN_SCALE
    w, h = int(rect.width * scale), int(rect.height * scale)
    img = Image.new("L", (w, h), 255)
    ImageDraw.Draw(img).text((4, 2), text, fill=0, font=ImageFont.load_default(size=int(h * 0.75)))
    step = w // parts
    for i in range(parts):
        piece = img.crop((i * step, 0, w if i == parts - 1 else (i + 1) * step, h))
        buf = io.BytesIO()
        piece.save(buf, "PNG")
        x0 = rect.x0 + i * step / scale
        x1 = rect.x1 if i == parts - 1 else rect.x0 + (i + 1) * step / scale
        page.insert_image(pymupdf.Rect(x0, rect.y0, x1, rect.y1), stream=buf.getvalue())


def _arrow(page, tail, head) -> None:
    sh = page.new_shape()
    sh.draw_line(tail, (head[0] - 6, head[1]))
    sh.finish(color=(0, 0, 0), width=0.8)
    sh.draw_polyline([(head[0] - 6, head[1] - 3), head, (head[0] - 6, head[1] + 3), (head[0] - 6, head[1] - 3)])
    sh.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.5)
    sh.commit()


def _tile_diagram_pdf(path: Path) -> Path:
    pdf = pymupdf.open()
    pg = pdf.new_page(width=595, height=842)
    for r, title in ((pymupdf.Rect(60, 100, 230, 330), "LampMaster"), (pymupdf.Rect(370, 100, 540, 330), "LampDriver")):
        pg.draw_rect(r, color=(0, 0, 0), width=1)
        pg.insert_text((r.x0 + 45, r.y0 + 20), title, fontname="hebo", fontsize=11)
    names = [("PpLampCmd", "RpLampCmd", "CmdLamp", "command"), ("PpDimCmd", "RpDimCmd", "CmdDim", "command"),
             ("PpLampMode", "RpLampMode", "ReqLampMode", "request")]
    for k, (src, dst, sig, kind) in enumerate(names):
        y = 170 + k * 50
        _tile(pg, pymupdf.Rect(162, y - 9, 228, y - 1), src)          # inside the left box, at its right border
        _tile(pg, pymupdf.Rect(372, y - 9, 438, y - 1), dst)          # inside the right box, at its left border
        _tile(pg, pymupdf.Rect(250, y - 11, 300, y - 2), sig)         # signal label above the arrow
        _tile(pg, pymupdf.Rect(306, y - 11, 344, y - 2), kind, parts=1)
        _arrow(pg, (230, y), (370, y))
    text = pdf.new_page(width=595, height=842)
    text.insert_text((60, 100), "The LampMaster sends " + ", ".join(n for row in names for n in row[:3]) + ".",
                     fontsize=9)
    pdf.save(str(path))
    return path


needs_tesseract = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")


@needs_tesseract
def test_diagram_labels_stored_as_image_tiles_are_read_and_verified(tmp_path):
    path = _tile_diagram_pdf(tmp_path / "tiles.pdf")
    with pymupdf.open(str(path)) as pdf:
        assert not diagram.analyze_page(pdf[0], 1).ports  # without the tile rule: no labels at all
        pd = diagram.analyze_page(pdf[0], 1, diagram.doc_vocab(pdf))
    ports = {(p.name, p.box.title, p.direction) for p in pd.ports}
    assert ("PpLampCmd", "LampMaster", "provided") in ports and ("RpLampCmd", "LampDriver", "required") in ports
    sigs = {(s.name, s.elem, s.sender.title if s.sender else None) for s in pd.signals}
    assert ("CmdLamp", "command", "LampMaster") in sigs
    assert diagram.page_qualifies(pd)


@needs_tesseract
def test_tile_entities_are_medium_and_say_they_were_read_by_ocr(tmp_path):
    path = _tile_diagram_pdf(tmp_path / "tiles.pdf")
    doc = {"doc_id": "tiles", "version": "1.0",
           "sections": [{"section_id": "1", "title": "Lamps", "page_start": 1, "page_end": 2, "text": ""}]}
    ents = diagram.extract_diagram_entities(doc, path)
    port = next(p for p in ents["ports"] if p.name == "PpLampCmd")
    assert port.confidence == "Medium" and "read by OCR" in port.description


@needs_tesseract
def test_text_layer_reading_wins_over_an_ocr_reading_of_the_same_signal(tmp_path):
    """Same figure twice: page 1 with image-tile labels, page 2 with real text. The merged signal keeps the text
    reading's page and confidence (the OCR page only adds a source), although page 1 comes first."""
    path = _tile_diagram_pdf(tmp_path / "tiles.pdf")
    with pymupdf.open(str(path)) as pdf:
        pg = pdf.new_page(-1, width=595, height=842)
        pdf.move_page(pdf.page_count - 1, 1)  # text-label copy becomes page 2
        pg = pdf[1]
        for r, title in ((pymupdf.Rect(60, 100, 230, 330), "LampMaster"), (pymupdf.Rect(370, 100, 540, 330), "LampDriver")):
            pg.draw_rect(r, color=(0, 0, 0), width=1)
            pg.insert_text((r.x0 + 45, r.y0 + 20), title, fontname="hebo", fontsize=11)
        for k, (src, dst, sig, kind) in enumerate([("PpLampCmd", "RpLampCmd", "CmdLamp", "command"),
                                                   ("PpDimCmd", "RpDimCmd", "CmdDim", "command")]):
            y = 170 + k * 50
            pg.insert_text((165, y - 2), src, fontsize=7)
            pg.insert_text((374, y - 2), dst, fontsize=7)
            pg.insert_text((250, y - 3), f"{sig} / {kind}", fontsize=7)
            _arrow(pg, (230, y), (370, y))
        pdf.save(str(tmp_path / "both.pdf"))
    doc = {"doc_id": "both", "version": "1.0", "sections": [{"section_id": "1", "title": "Lamps", "page_start": 1,
                                                             "page_end": 3, "text": ""}]}
    ents = extract.extract_entities(doc, pdf_path=tmp_path / "both.pdf", use_convention=False)
    sig = next(s for s in ents["signals"] if s.name == "CmdLamp")
    assert sig.sources[0].page == 2 and sig.confidence == "High" and {s.page for s in sig.sources} == {1, 2}
    assert [x.ocr for x in sig.sources] == [False, True]  # the OCR-read page is kept, marked, and listed last


def test_row_page_comes_from_a_text_source_before_an_ocr_dependent_one():
    S = extract.Source
    items = [extract.Component(name="AutoAdjustUserRequestManager", extraction_method="heuristic", confidence="Medium",
                               sources=[S(doc_id="d", page=26, section="4.4.2.4 x", method="heuristic")]),
             extract.Component(name="AutoAdjustUserRequestManager", extraction_method="diagram", confidence="Medium",
                               sources=[S(doc_id="d", page=11, section="4.2.1 x", method="diagram", ocr=True)])]
    (m,) = extract.merge_entities(items)
    assert m.sources[0].page == 26 and m.extraction_method == "diagram" and len(m.sources) == 2


def test_unverified_tile_labels_are_dropped(tmp_path, monkeypatch):
    path = _tile_diagram_pdf(tmp_path / "tiles.pdf")
    with pymupdf.open(str(path)) as pdf:
        assert diagram._tile_lines(pdf[0], {"SomethingElse"}) == []  # nothing verified -> nothing kept
        assert diagram._tile_lines(pdf[0], None) == []


# ---------------------------------------------------------------- 2. composition groups
def _doc(sections):
    return {"doc_id": "d", "version": "1.0", "sections": [
        {"section_id": sid, "title": t, "page_start": p, "page_end": p, "text": txt, "tables": []}
        for sid, t, p, txt in sections]}


def test_composition_group_gets_children_from_its_own_section():
    doc = _doc([("4", "Description of Software Compositions", 8, "The following compositions were identified."),
                ("4.1", "Wiper/Washer", 8, "The WiperWasher software component group controls the wiper."),
                ("4.1.1", "Rain sensing", 10, "")])
    src = lambda sid: [extract.Source(doc_id="d", page=9, section=f"{sid} x")]  # noqa: E731
    comps = [extract.Component(name="FrontWiper", sources=src("4.1")),
             extract.Component(name="RainSensor", sources=src("4.1.1")),
             extract.Component(name="InteriorLightManager", sources=src("4.3")),
             extract.Component(name="CentralLocking (incl. Doorlocks) / Remote Key", sources=src("4.1"))]
    (g,) = extract.composition_groups(doc, comps)
    assert (g.name, g.category, g.children) == ("WiperWasher", "composition", ["FrontWiper", "RainSensor"])
    assert g.sources[0].section.startswith("4.1 ") and g.sources[0].page == 8


def test_composition_rules_skip_multi_word_names_and_passing_mentions():
    doc = _doc([("2.2", "Overview", 5, "An ESC ECU may contain the ESC SW-Composition and the SSM SW-Composition. "
                                       "The High Level Steering Driver Assistance System SW-Composition is a place "
                                       "holder."),
                ("4.1", "ACC", 12, "The SW-Composition ACC controls the vehicle velocity.")])
    comps = [extract.Component(name="Sub", sources=[extract.Source(doc_id="d", section="2.2 Overview")])]
    groups = {g.name: g for g in extract.composition_groups(doc, comps)}
    assert set(groups) == {"ESC", "SSM", "ACC"}  # not 'System' (last word of a multi-word name)
    assert groups["ESC"].children == [] and groups["ACC"].sources[0].section == "4.1 ACC"


def test_composition_is_stored_and_findable_by_name(tmp_path):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    extract.ensure_schema(c)
    c.execute("INSERT INTO components(project_id, doc_id, version, name, norm_name, fields, page, section, "
              "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("p", "d", "1.0", "WiperWasher", "wiperwasher",
               json.dumps({"category": "composition", "children": ["FrontWiper", "FrontWasher"]}), 8,
               "4.1 Wiper/Washer", "[]", "heuristic", "Medium", "unreviewed"))
    inv = extract.get_inventory("p", conn=c)
    q = "Which ports does the WiperWasher have?"
    info = retrieve.analyze_query(q, [], retrieve.inventory_names(inv))
    assert info.components == ["wiperwasher"]
    (hit,) = retrieve.inventory_evidence("p", info, c, [{"doc_id": "d", "version": "1.0", "title": None,
                                                         "family": "d"}], inv=inv)
    assert hit["rows"]["children"] == ["FrontWiper", "FrontWasher"]
    assert "software composition containing: FrontWiper, FrontWasher" in hit["text"]


# ---------------------------------------------------------------- 3. interfaces named in prose
def test_interfaces_named_in_prose():
    doc = _doc([("4.3.12", "Known Deficiencies", 21,
                 "Please note that all ports referencing the interface DiagnosticMonitor are client ports. The ports "
                 "referencing the interfaces DiagSensor, Diag- Brightness are defined within HMI."),
                ("4.3.11", "List", 18, "These groups of interior light interfaces are defined (virtual interior light "
                                       "interfaces): IntLightGroup0, IntLightGroup1, … IntLightGroup15, Each group."),
                ("5.2", "ACC", 14, "Acceleration interface ACC User interface. The interface from user operations.")])
    names = {e.name for e in extract.prose_interfaces(doc)}
    assert names == {"DiagnosticMonitor", "DiagSensor", "DiagBrightness", "IntLightGroup0", "IntLightGroup1",
                     "IntLightGroup15"}  # 'ACC' (bare acronym) and lower-case words are not interfaces
    e = next(x for x in extract.prose_interfaces(doc) if x.name == "DiagnosticMonitor")
    assert e.extraction_method == "heuristic" and e.confidence == "Medium" and "DiagnosticMonitor" in e.sources[0].evidence


# ---------------------------------------------------------------- 4. "no ports extracted" answer
def _gap_lmap(children=()):
    gap = {"id": "inv:d:wiperwasher", "score": 1.0, "text": "EXTRACTED INVENTORY ... No ports ...",
           "rows": {"kind": "gap", "component": "WiperWasher", "children": list(children), "section": "4.1 Wiper/Washer"},
           "metadata": {"doc_id": "bodycomfort_r3.2.3", "page_start": 8, "page_end": 8, "section_id": "4.1",
                        "inventory": True, "extraction_gap": "WiperWasher"}}
    text = {"id": "bc::4.1::0", "score": 0.71, "text": "The WiperWasher software component group controls the wiper.",
            "metadata": {"doc_id": "bodycomfort_r3.2.3", "page_start": 8, "page_end": 9, "section_id": "4.1",
                         "section_title": "Wiper/Washer"}}
    text2 = {"id": "bc::4.1.1::0", "score": 0.66, "text": "Rain sensing consists of the Rain Sensor.",
             "metadata": {"doc_id": "bodycomfort_r3.2.3", "page_start": 10, "page_end": 10, "section_id": "4.1.1",
                          "section_title": "Rain sensing"}}
    return qa.label_map([gap, text, text2])


def test_gap_answer_text_and_citations():
    text, labels = qa.gap_answer(_gap_lmap())
    assert text.startswith("No ports were extracted for WiperWasher in bodycomfort_r3.2.3; this may be a gap in "
                           "extraction. Closest sections: §4.1 Wiper/Washer, p.8 [C1]; §4.1.1 Rain sensing, p.10 [C3].")
    assert labels == ["C1", "C3"]  # C2 is the same section as the component's own: cited once
    text, labels = qa.gap_answer(_gap_lmap(["FrontWiper", "FrontWasher"]))
    assert "WiperWasher is a software composition containing FrontWiper, FrontWasher [C1]." in text
    assert text.count("[C1]") == 1 and "§4.1 Wiper/Washer, p.8;" in text  # cited once, not repeated
    assert qa.gap_answer({k: v for k, v in _gap_lmap().items() if k != "C1"}) is None


class _Ret:
    def __init__(self, hits):
        self.hits, self.top_score, self.on_topic, self.exact_identifier = hits, 0.71, True, False
        self.coverage, self.missing_terms, self.searched, self.release_note = 1.0, [], ["bc"], ""
        self.query = retrieve.analyze_query("Which ports does the WiperWasher have?", [], {})


class _Fake:
    model = "fake"

    def __init__(self, reply):
        self.reply = reply

    def generate(self, prompt, system="", model=None, purpose="", chunk_ids=None):
        return self.reply


@pytest.mark.parametrize("reply", ["Not stated in the provided documents.", "WiperWasher has no ports [C2].",
                                   "Something without a label."])
def test_ask_names_the_extraction_gap_instead_of_a_bare_refusal(monkeypatch, reply):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(controls.SCHEMA)
    hits = list(_gap_lmap().values())
    monkeypatch.setattr(qa.retrieve, "retrieve", lambda *a, **k: _Ret(hits))
    monkeypatch.setattr(qa.retrieve, "embedder_status", lambda: {"fallback": False})
    monkeypatch.setattr(controls, "require_role", lambda *a, **k: None)
    a = qa.ask("p", "Which ports does the WiperWasher have?", "u", conn=c, provider=_Fake(reply))
    assert (a.mode, a.refusal_reason, a.confidence) == ("refused", "not_extracted", "Low")
    assert a.answer.startswith("No ports were extracted for WiperWasher") and "has no ports" not in a.answer
    assert [x.label for x in a.citations] == ["C1", "C3"] and a.model == ""
    assert "model produced no sentence" not in a.limitation


def test_a_real_cited_answer_from_the_text_is_kept(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(controls.SCHEMA)
    hits = list(_gap_lmap().values())
    monkeypatch.setattr(qa.retrieve, "retrieve", lambda *a, **k: _Ret(hits))
    monkeypatch.setattr(qa.retrieve, "embedder_status", lambda: {"fallback": False})
    monkeypatch.setattr(controls, "require_role", lambda *a, **k: None)
    a = qa.ask("p", "Which ports does the WiperWasher have?", "u", conn=c,
               provider=_Fake("The WiperWasher software component group controls the wiper [C2]."))
    assert a.mode == "generated" and a.built_from == "llm"


# ---------------------------------------------------------------- 5. closest passages (node)
NODE = shutil.which("node")


def _node(script: str):
    js = (ROOT / "app/static/js/answer_view.js").as_uri()
    out = subprocess.run([NODE, "--input-type=module", "-e", f"import * as v from {json.dumps(js)};\n{script}"],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_closest_passages_label_and_when_shown():
    got = _node("""const ev = [{label: "C1"}];
      console.log(JSON.stringify({label: v.CLOSEST_LABEL, shown: [
        v.showClosest({mode: "refused", refusal_reason: "not_found", evidence: ev}),
        v.showClosest({mode: "refused", refusal_reason: "off_topic", evidence: ev}),
        v.showClosest({mode: "refused", refusal_reason: "not_extracted", evidence: ev}),
        v.showClosest({mode: "refused", refusal_reason: "not_found", evidence: []}),
        v.showClosest({mode: "generated", evidence: ev})]}));""")
    assert got == {"label": "Closest passages searched (not used in an answer)", "shown": [True, False, False, False, False]}


def test_closest_passages_block_has_no_citation_tags():
    js = (ROOT / "app/static/js/ask.js").read_text(encoding="utf-8")
    block = js[js.index("const closest = showClosest(a)"):js.index("const title = gap")]
    assert "e.label" not in block and "cite" not in block and "<details" not in block
    assert "Closest passages that were searched" not in js


# ---------------------------------------------------------------- 6 + 7. Documents page and isolation
@pytest.fixture
def client(tmp_path, conn, dirs):
    from fastapi.testclient import TestClient

    from app import api
    conn.close()
    api.app.dependency_overrides[api.get_paths] = lambda: api.Paths(tmp_path / "test.db", dirs["chroma_dir"],
                                                                     dirs["processed_dir"])
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _upload(client, path, pid):
    with open(path, "rb") as f:
        return client.post(f"/projects/{pid}/upload", headers={"X-User": "admin"},
                           files={"file": (path.name, f, "application/pdf")},
                           data={"doc_type": "hld", "version": "1.0", "source_status": "approved"})


def test_projects_report_document_counts_of_member_projects_only(client, tmp_path, make_pdf):
    assert _upload(client, make_pdf(tmp_path / "a.pdf", marker="alpha"), "p1").status_code == 200
    rows = {p["project_id"]: p["documents"] for p in client.get("/projects", headers={"X-User": "view"}).json()}
    assert rows == {"p1": 1, "p2": 0}
    db = controls.get_conn(tmp_path / "test.db")
    controls.create_project("p3", conn=db)
    controls.add_user("solo", "viewer", conn=db)
    controls.add_member("p3", "solo", conn=db)
    db.close()
    assert [p["project_id"] for p in client.get("/projects", headers={"X-User": "solo"}).json()] == ["p3"]


def test_documents_page_has_scope_line_and_settings_button(client):
    html = client.get("/documents?project=p1", cookies={"hld_user": "admin"}).text
    assert 'id="docs-scope"' in html and 'id="open-settings"' in html and "Change project in" in html
    js = (ROOT / "app/static/js/documents.js").read_text(encoding="utf-8")
    assert "openSettings" in js and "of ${total} document" in js


def test_reports_exports_and_pages_never_cross_projects(client, tmp_path, make_pdf):
    assert _upload(client, make_pdf(tmp_path / "other_hld.pdf", marker="zuluquebec"), "p2").status_code == 200
    other = client.get("/projects/p2/documents", headers={"X-User": "admin"}).json()[0]["doc_id"]
    h = {"X-User": "admin"}
    for url in (f"/projects/p1/reports/flow?doc_id={other}", f"/projects/p1/reports/document?doc_id={other}",
                f"/projects/p1/graph?doc_id={other}", f"/projects/p1/documents/{other}/pages/1.png",
                f"/projects/p1/export/diff?doc_a={other}&doc_b={other}&fmt=json"):
        r = client.get(url, headers=h)
        assert r.status_code >= 400 and "zuluquebec" not in r.text, url  # content of p2 never returned
    r = client.get(f"/projects/p1/export?kind=inventory&fmt=json&doc_id={other}", headers=h)
    assert r.status_code == 200 and other not in r.text  # filtered to p1: no p2 rows
    a = client.post("/projects/p1/ask", headers=h, json={"question": "What are the zuluquebec interfaces?"}).json()
    assert "zuluquebec" not in json.dumps(a["evidence"]) and not a["citations"]
