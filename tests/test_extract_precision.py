"""B5 precision rules (extract.refine_entities): type annotations, compound names, name-only llm quotes,
near-duplicate components."""
from app import extract
from app.extract import Component, Interface, Port, Source


def _c(name, method="heuristic", evidence=""):
    return Component(name=name, extraction_method=method,
                     sources=[Source(doc_id="d", page=1, method=method, evidence=evidence)])


def _names(ents, t="components"):
    return sorted(e.name for e in ents[t])


def test_type_annotation_names_dropped_but_instance_type_kept():
    ents = {"components": [_c("Rain::RainSensor")], "ports": [],
            "interfaces": [Interface(name="DcmDspSecurityLevel: EcucIntegerParamDef"), Interface(name="IWiper")],
            "signals": []}
    out = extract.refine_entities(ents)
    assert _names(out, "interfaces") == ["IWiper"] and _names(out) == ["Rain::RainSensor"]


def test_compound_heading_names_dropped():
    ents = {"components": [_c("Doors, KeylessAccess and RemoteKey"), _c("CentralLocking (incl. Doorlocks) / Remote Key"),
                           _c("Mirror Adjustment& Tinting"), _c("SeatAdjustManager")], "ports": [], "interfaces": [],
            "signals": []}
    assert _names(extract.refine_entities(ents)) == ["SeatAdjustManager"]


def test_llm_component_with_name_only_quote_dropped():
    ents = {"components": [_c("Exterior Lights", "llm", "Exterior Lights"),
                           _c("WasherFluidTank", "llm", "WasherFluidTank senses the level of the washer fluid")],
            "ports": [], "interfaces": [], "signals": []}
    assert _names(extract.refine_entities(ents)) == ["WasherFluidTank"]


def test_near_duplicates_merged_under_best_name():
    ents = {"components": [_c("Interior Light Sensors", "heuristic"), _c("Interior Light Sensor", "diagram"),
                           _c("RSC Roll Stability Control", "llm", "RSC Roll Stability Control prevents rollover"),
                           _c("RSC", "heuristic"), _c("Central Locking", "diagram"), _c("CentralLocking", "diagram")],
            "ports": [], "interfaces": [], "signals": []}
    out = extract.refine_entities(ents)
    assert _names(out) == ["CentralLocking", "Interior Light Sensor", "RSC"]
    sensor = next(e for e in out["components"] if e.name == "Interior Light Sensor")
    assert {s.method for s in sensor.sources} == {"heuristic", "diagram"}


def test_component_key():
    assert extract.component_key("RSC Roll Stability Control") == "rsc"
    assert extract.component_key("Exterior Lights") == extract.component_key("Exterior Light")
    assert extract.component_key("Access") == "access"  # 'ss' is not a plural


def test_ports_untouched_by_component_rules():
    p = Port(name="ReqWiper/Washer", component="X")
    out = extract.refine_entities({"components": [], "ports": [p], "interfaces": [], "signals": []})
    assert out["ports"] == [p]


def test_diagram_colon_junk_never_survives():
    ents = {"components": [_c(": : :1 00 1234", "diagram"), _c(": Process :", "diagram"), _c("Dcm", "diagram")],
            "ports": [], "interfaces": [], "signals": []}
    assert _names(extract.refine_entities(ents)) == ["Dcm"]
