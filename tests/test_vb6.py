"""VB6 fork behavior: useful source indexing without designer/call-graph noise."""
from pathlib import Path
import shutil

from graphify.extract import collect_files, extract, _get_extractor
from graphify.extractors.vb6 import extract_vb6, extract_vb6_project
from graphify.validate import validate_extraction


FIXTURE = Path(__file__).parent / "fixtures" / "vb6"


def _edges(result, relation):
    labels = {n["id"]: n["label"] for n in result["nodes"]}
    return {(labels[e["source"]], labels[e["target"]])
            for e in result["edges"] if e["relation"] == relation}


def _source(tmp_path, text, name="Example.bas", encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


def test_project_pipeline_and_cache(tmp_path):
    from graphify.detect import detect
    from graphify.watch import _WATCHED_EXTENSIONS
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    code = detect(root)["files"]["code"]
    assert {Path(p).suffix for p in code} == {".bas", ".frm", ".cls", ".vbp"}
    assert {".bas", ".frm", ".cls", ".vbp"} <= _WATCHED_EXTENSIONS
    files = collect_files(root)
    assert len(files) == 4
    first = extract(files, root=root, cache_root=tmp_path, parallel=False)
    second = extract(files, root=root, cache_root=tmp_path, parallel=False)
    assert first == second
    assert validate_extraction(first) == []
    from graphify.build import build_from_json
    built = build_from_json(first, directed=True, root=root)
    # Project membership references reconcile to the actual source file nodes.
    assert len([n for _, n in built.nodes(data=True) if n["label"] == "Customer.cls"]) == 1
    assert {("Sample.vbp", name) for name in ("Utilities.bas", "Customer.cls", "Main.frm")} <= _edges(first, "contains")
    assert ("Main()", "Prepare()") in _edges(first, "calls")
    assert ("Save()", "Validate()") in _edges(first, "calls")
    assert ("Customer", "IPersist") in _edges(first, "implements")
    # Cross-file object dispatch has deliberately not been inferred by name.
    assert ("cmdSave_Click()", "Save()") not in _edges(first, "calls")
    assert all(not Path(n["source_file"]).is_absolute() for n in first["nodes"])
    assert not any("cmdSave" == n["label"] for n in first["nodes"])


def test_declarations_properties_and_source_locations():
    result = extract_vb6(FIXTURE / "Customer.cls")
    labels = {n["label"]: n for n in result["nodes"]}
    assert {"Name [Get]", "Name [Let]", "Owner [Set]", "mName", "Saved"} <= labels.keys()
    assert labels["Save()"]["source_location"] == "L22"
    assert len({labels[n]["id"] for n in ("Name [Get]", "Name [Let]")}) == 2
    assert validate_extraction(result) == []
    data = extract_vb6(FIXTURE / "Utilities.bas")
    assert {"DefaultName", "CustomerRecord", "CustomerStatus", "Active", "Sleep()"} <= {n["label"] for n in data["nodes"]}
    assert ("Main()", "Sleep()") in _edges(data, "calls")


def test_comments_continuations_colons_and_case(tmp_path):
    path = _source(tmp_path, '''Attribute VB_Name = "Example"
Public Sub Run()
    Rem Helper(10)
    Debug.Print "Helper(1): ' quoted ""string"""
    ' Call Helper
    Call _
        hElPeR(1)
    Helper 2: Rem Helper(10): Helper 10
    If True Then Helper 3 Else Helper 4
End Sub
Private Sub Helper(ByVal value As Long)
End Sub
''')
    result = extract_vb6(path)
    calls = [e for e in result["edges"] if e["relation"] == "calls"]
    assert {e["source_location"] for e in calls} == {"L6", "L8", "L9"}


def test_local_calls_do_not_guess_members_arrays_or_shadowed_names(tmp_path):
    path = _source(tmp_path, '''Sub Run(ByVal Helper As Object)
    Helper(1)
End Sub
Sub Arrays()
    Dim Helper(4) As Long
    Helper(1) = 2
End Sub
Sub Members()
    obj.Helper(1)
    obj . Helper(1)
    obj.Helper 1
    With obj
        .Helper(1)
    End With
End Sub
Sub Helper(ByVal value As Long)
End Sub
''')
    assert not _edges(extract_vb6(path), "calls")


def test_conditional_definitions_are_not_arbitrarily_resolved(tmp_path):
    path = _source(tmp_path, '''#If DEBUG Then
Sub Helper()
End Sub
#Else
Sub Helper()
End Sub
#End If
Sub Run()
    Helper
End Sub
''')
    result = extract_vb6(path)
    assert len([n for n in result["nodes"] if n["label"] == "Helper()"]) == 2
    assert not _edges(result, "calls")


def test_windows_encoding_and_uppercase_extension(tmp_path):
    path = _source(tmp_path, 'Attribute VB_Name = "Cobrança"\nPublic Sub Ação()\nEnd Sub\n', "Billing.CLS", "cp1252")
    assert _get_extractor(path) is extract_vb6
    assert {"Cobrança", "Ação()"} <= {n["label"] for n in extract_vb6(path)["nodes"]}


def test_project_missing_member_is_a_reference_not_a_read(tmp_path):
    path = _source(tmp_path, 'Name="Demo"\nModule=Missing; sub\\Missing.bas\n', "Demo.vbp")
    result = extract_vb6_project(path)
    assert _edges(result, "contains") == {("Demo.vbp", "Missing.bas")}
    assert validate_extraction(result) == []


def test_apex_cache_is_replaced_for_cls(tmp_path):
    from graphify.cache import save_cached
    path = _source(tmp_path, 'Attribute VB_Name = "Customer"\nSub Save()\nEnd Sub\n', "Customer.cls")
    stale = {"nodes": [{"id": "apex", "label": "Apex", "file_type": "code", "source_file": str(path)}], "edges": []}
    save_cached(path, stale, tmp_path, cache_root=tmp_path)
    result = extract([path], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert "Save()" in {n["label"] for n in result["nodes"]}
    assert "Apex" not in {n["label"] for n in result["nodes"]}


def test_empty_and_unreadable_files(tmp_path):
    assert extract_vb6(tmp_path / "missing.bas")["error"]
    assert extract_vb6_project(tmp_path / "missing.vbp")["error"]
    assert validate_extraction(extract_vb6(_source(tmp_path, ""))) == []


def test_local_constants_belong_to_their_procedure_and_rem_after_then(tmp_path):
    path = _source(tmp_path, '''Sub Run()
    Const Value As Long = 1
    If True Then Rem Helper(1)
End Sub
Sub Helper(ByVal value As Long)
End Sub
''')
    result = extract_vb6(path)
    assert ("Run()", "Value") in _edges(result, "contains")
    assert not _edges(result, "calls")


def test_update_replaces_changed_vb6_symbols(tmp_path, monkeypatch):
    from graphify.watch import _rebuild_code
    import json
    monkeypatch.chdir(tmp_path)
    path = _source(tmp_path, "Sub Before()\nEnd Sub\n", "Code.bas")
    assert _rebuild_code(tmp_path, no_cluster=True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    assert "Before()" in {n["label"] for n in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]}
    path.write_text("Sub After()\nEnd Sub\n", encoding="utf-8")
    assert _rebuild_code(tmp_path, changed_paths=[path], no_cluster=True)
    labels = {n["label"] for n in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]}
    assert "After()" in labels
    assert "Before()" not in labels


def test_form_and_module_with_same_stem_stay_distinct(tmp_path):
    bas = _source(tmp_path, 'Attribute VB_Name = "SharedModule"\nSub Run()\nEnd Sub\n', "Shared.bas")
    frm = _source(tmp_path, 'Attribute VB_Name = "SharedForm"\nSub Run()\nEnd Sub\n', "Shared.frm")
    result = extract([bas, frm], root=tmp_path, cache_root=tmp_path, parallel=False)
    assert len({n["id"] for n in result["nodes"] if n["label"] == "Run()"}) == 2
    assert validate_extraction(result) == []
