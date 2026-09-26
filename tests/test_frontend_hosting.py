from fastapi.testclient import TestClient

from medical_rag.api import create_app


def test_frontend_deep_links_and_assets_are_served_without_intercepting_api(tmp_path):
    class Service:
        def nodes(self):
            return [{"type": "query.input"}]

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>知源工作台</html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("// app", encoding="utf-8")
    api = TestClient(create_app(service=Service(), frontend_dist=tmp_path))

    assert api.get("/ui/chat").text == "<html>知源工作台</html>"
    assert api.get("/ui/workflows/medical-answer").status_code == 200
    assert api.get("/ui/assets/app.js").text == "// app"
    assert api.get("/ui/assets/missing.js").status_code == 404
    assert api.get("/nodes").json()["data"] == [{"type": "query.input"}]
