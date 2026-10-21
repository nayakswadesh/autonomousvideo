import json

from scripts.prepare_github_export import scan


def test_publish_scan_reports_location_not_credential_value(tmp_path):
    path = tmp_path / "unsafe.txt"
    synthetic = "sk-proj-" + "X" * 48
    path.write_text("example\n" + synthetic, encoding="utf-8")
    findings = scan([path], tmp_path)
    assert findings == [("unsafe.txt", "OpenAI-like key", 2)]
    assert synthetic not in repr(findings)


def test_publish_scan_flags_oauth_json_but_allows_blank_template(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "x" * 40}), encoding="utf-8")
    template = tmp_path / ".env.example"
    template.write_text("OPENAI_API_KEY=\nGEMINI_API_KEY=\n", encoding="utf-8")
    assert scan([token], tmp_path) == [("token.json", "credential-bearing JSON field", 0)]
    assert scan([template], tmp_path) == []
