from swarn_backend.storage.keys import content_type_for, file_kind, upload_key, workspace_file_key
from swarn_backend.util import clean_relative_path, sanitize_upload_name


def test_clean_relative_path():
    assert clean_relative_path("plots/x.png") == "plots/x.png"
    assert clean_relative_path("./plots//x.png") == "plots/x.png"
    assert clean_relative_path("") == ""
    assert clean_relative_path("../etc/passwd") is None
    assert clean_relative_path("/etc/passwd") is None
    assert clean_relative_path("a/../../b") is None


def test_sanitize_upload_name_keeps_extension():
    assert sanitize_upload_name("movies.csv") == "movies.csv"
    assert sanitize_upload_name("my data (1).xlsx") == "my_data__1_.xlsx"
    assert sanitize_upload_name("../../evil") == "evil"
    assert sanitize_upload_name(None) == "upload.dat"


def test_file_kinds():
    assert file_kind("plots/a.png") == "chart"
    assert file_kind("plots/a.vl.json") == "spec"
    assert file_kind("sales_report.html") == "report"
    assert file_kind("out/table.parquet") == "table"
    assert file_kind("artifacts/m.pkl") == "model"
    assert file_kind("notes.txt") == "other"


def test_keys_and_content_types():
    assert upload_key("u", "b", "f.csv") == "users/u/uploads/b/f.csv"
    assert workspace_file_key("w", "plots/a.png") == "workspaces/w/files/plots/a.png"
    assert content_type_for("x.vl.json") == "application/json"
    assert content_type_for("x.parquet") == "application/vnd.apache.parquet"
    assert content_type_for("x.png") == "image/png"
