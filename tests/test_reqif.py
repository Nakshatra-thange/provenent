import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from conftest import edit
from provenant.project import Project
from provenant.reqif import (
    ReqIFError,
    ReqRecord,
    diff_records,
    export_reqif,
    import_reqif,
    load_records,
    merge_into_yaml,
    records_from_project,
)


def test_round_trip_preserves_everything(demo, tmp_path):
    records = records_from_project(Project.load(demo))
    path = tmp_path / "te2.reqif"
    path.write_bytes(export_reqif(records, "TE-2"))
    imported = import_reqif(path)
    assert imported == records
    assert diff_records(records, imported).empty


def test_export_is_well_formed_reqif(demo):
    data = export_reqif(records_from_project(Project.load(demo)), "TE-2")
    root = ET.fromstring(data)
    ns = "{http://www.omg.org/spec/ReqIF/20110401/reqif.xsd}"
    assert root.tag == f"{ns}REQ-IF"
    assert len(root.findall(f".//{ns}SPEC-OBJECT")) == 13
    assert root.find(f".//{ns}REQ-IF-HEADER/{ns}TITLE").text == "TE-2"
    # Schema order: CHILDREN precedes OBJECT inside SPEC-HIERARCHY.
    top = root.find(f".//{ns}SPECIFICATION/{ns}CHILDREN/{ns}SPEC-HIERARCHY")
    assert [c.tag.split("}")[1] for c in top] == ["CHILDREN", "OBJECT"]


SUPPLIER = """<?xml version="1.0" encoding="UTF-8"?>
<REQ-IF xmlns="http://www.omg.org/spec/ReqIF/20110401/reqif.xsd" xmlns:xhtml="http://www.w3.org/1999/xhtml">
 <CORE-CONTENT><REQ-IF-CONTENT>
  <DATATYPES>
   <DATATYPE-DEFINITION-STRING IDENTIFIER="s" LONG-NAME="String" MAX-LENGTH="100"/>
   <DATATYPE-DEFINITION-XHTML IDENTIFIER="x" LONG-NAME="XHTML"/>
   <DATATYPE-DEFINITION-ENUMERATION IDENTIFIER="e" LONG-NAME="Methods">
    <SPECIFIED-VALUES><ENUM-VALUE IDENTIFIER="ev-test" LONG-NAME="Test"/></SPECIFIED-VALUES>
   </DATATYPE-DEFINITION-ENUMERATION>
  </DATATYPES>
  <SPEC-TYPES><SPEC-OBJECT-TYPE IDENTIFIER="t" LONG-NAME="Req"><SPEC-ATTRIBUTES>
   <ATTRIBUTE-DEFINITION-STRING IDENTIFIER="a-id" LONG-NAME="Object Identifier"/>
   <ATTRIBUTE-DEFINITION-STRING IDENTIFIER="a-head" LONG-NAME="Object Heading"/>
   <ATTRIBUTE-DEFINITION-XHTML IDENTIFIER="a-text" LONG-NAME="ReqIF.Text"/>
   <ATTRIBUTE-DEFINITION-ENUMERATION IDENTIFIER="a-m" LONG-NAME="Verification Method"/>
   <ATTRIBUTE-DEFINITION-STRING IDENTIFIER="a-x" LONG-NAME="Supplier Owner"/>
  </SPEC-ATTRIBUTES></SPEC-OBJECT-TYPE></SPEC-TYPES>
  <SPEC-OBJECTS>
   <SPEC-OBJECT IDENTIFIER="o1"><VALUES>
     <ATTRIBUTE-VALUE-STRING THE-VALUE="VLV-001"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>a-id</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
     <ATTRIBUTE-VALUE-STRING THE-VALUE="Main valve"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>a-head</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
     <ATTRIBUTE-VALUE-STRING THE-VALUE="Ops team"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>a-x</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
   </VALUES><TYPE><SPEC-OBJECT-TYPE-REF>t</SPEC-OBJECT-TYPE-REF></TYPE></SPEC-OBJECT>
   <SPEC-OBJECT IDENTIFIER="o2"><VALUES>
     <ATTRIBUTE-VALUE-STRING THE-VALUE="VLV-010"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>a-id</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
     <ATTRIBUTE-VALUE-XHTML><DEFINITION><ATTRIBUTE-DEFINITION-XHTML-REF>a-text</ATTRIBUTE-DEFINITION-XHTML-REF></DEFINITION>
       <THE-VALUE><xhtml:div><xhtml:p>Valve shall open in   <xhtml:b>under 80 ms</xhtml:b>.</xhtml:p><xhtml:p>Second para.</xhtml:p></xhtml:div></THE-VALUE></ATTRIBUTE-VALUE-XHTML>
     <ATTRIBUTE-VALUE-ENUMERATION><DEFINITION><ATTRIBUTE-DEFINITION-ENUMERATION-REF>a-m</ATTRIBUTE-DEFINITION-ENUMERATION-REF></DEFINITION>
       <VALUES><ENUM-VALUE-REF>ev-test</ENUM-VALUE-REF></VALUES></ATTRIBUTE-VALUE-ENUMERATION>
   </VALUES><TYPE><SPEC-OBJECT-TYPE-REF>t</SPEC-OBJECT-TYPE-REF></TYPE></SPEC-OBJECT>
   <SPEC-OBJECT IDENTIFIER="o3"><VALUES>
     <ATTRIBUTE-VALUE-STRING THE-VALUE="VLV-099"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>a-id</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>
   </VALUES><TYPE><SPEC-OBJECT-TYPE-REF>t</SPEC-OBJECT-TYPE-REF></TYPE></SPEC-OBJECT>
  </SPEC-OBJECTS>
  <SPECIFICATIONS><SPECIFICATION IDENTIFIER="sp"><CHILDREN>
   <SPEC-HIERARCHY IDENTIFIER="h1"><CHILDREN><SPEC-HIERARCHY IDENTIFIER="h2"><OBJECT><SPEC-OBJECT-REF>o2</SPEC-OBJECT-REF></OBJECT></SPEC-HIERARCHY></CHILDREN>
    <OBJECT><SPEC-OBJECT-REF>o1</SPEC-OBJECT-REF></OBJECT>
   </SPEC-HIERARCHY>
  </CHILDREN></SPECIFICATION></SPECIFICATIONS>
 </REQ-IF-CONTENT></CORE-CONTENT>
</REQ-IF>
"""


def test_imports_other_tools_conventions(tmp_path):
    path = tmp_path / "supplier.reqif"
    path.write_text(SUPPLIER, encoding="utf-8")
    records = import_reqif(path)
    assert [r.id for r in records] == ["VLV-001", "VLV-010", "VLV-099"]
    assert records[0].title == "Main valve"
    assert records[1].parent == "VLV-001"
    assert records[1].text == "Valve shall open in under 80 ms.\nSecond para."
    assert records[1].method == "test"
    assert records[2].parent is None  # object outside the specification tree is still imported


def test_rejects_non_reqif(tmp_path):
    bad = tmp_path / "x.reqif"
    bad.write_text("<notreqif/>", encoding="utf-8")
    with pytest.raises(ReqIFError, match="expected <REQ-IF>"):
        import_reqif(bad)
    bad.write_text("<REQ-IF", encoding="utf-8")
    with pytest.raises(ReqIFError, match="not well-formed"):
        import_reqif(bad)
    with pytest.raises(ReqIFError, match="don't know how to read"):
        load_records(tmp_path / "spec.docx")


def test_duplicate_ids_rejected(tmp_path):
    with pytest.raises(ReqIFError, match="duplicate requirement ids A-1"):
        export_reqif([ReqRecord("A-1", "one"), ReqRecord("A-1", "two")], "dup")
    # A foreign file whose distinct objects share a ForeignID is rejected on import.
    data = export_reqif([ReqRecord("A-1", "one"), ReqRecord("A-2", "two")], "dup").decode()
    path = tmp_path / "dup.reqif"
    path.write_text(data.replace('THE-VALUE="A-2"', 'THE-VALUE="A-1"'), encoding="utf-8")
    with pytest.raises(ReqIFError, match="more than once"):
        import_reqif(path)


def test_diff_between_baselines(demo, tmp_path):
    before = tmp_path / "v1.reqif"
    before.write_bytes(export_reqif(records_from_project(Project.load(demo)), "TE-2"))
    edit(demo / "requirements.yaml", "<= 900 K", "<= 850 K")
    diff = diff_records(load_records(before), load_records(demo))
    assert list(diff.modified) == ["TCS-020"]
    change = diff.modified["TCS-020"][0]
    assert change.field == "condition" and "850 K" in change.after
    assert diff_records(load_records(demo / "requirements.yaml"), load_records(demo)).empty


def test_merge_keeps_local_fields_supplier_does_not_carry(demo):
    records = records_from_project(Project.load(demo))
    # A supplier round-trips the spec through a tool that drops Provenant.Condition
    # and edits one requirement's text.
    incoming = [replace(r, condition=None) for r in records]
    incoming = [replace(r, text="Edited by supplier.") if r.id == "STR-012" else r for r in incoming]
    diff = merge_into_yaml(demo / "requirements.yaml", incoming)
    assert list(diff.modified) == ["STR-012"]
    project = Project.load(demo)
    assert project.requirements["STR-012"].text == "Edited by supplier."
    assert project.requirements["STR-012"].condition.source == "value(dry_mass) <= 95 kg"


def test_merge_prune(demo):
    records = [r for r in records_from_project(Project.load(demo)) if r.id != "SYS-030"]
    diff = merge_into_yaml(demo / "requirements.yaml", records, prune=True)
    assert [r.id for r in diff.removed] == ["SYS-030"]
