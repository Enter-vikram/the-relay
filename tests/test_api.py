"""HTTP layer. Scripted model double; no network."""
from fastapi.testclient import TestClient

from app.chunking import RetrievalSetting
from app.gateway import GatewayResult
from app.main import create_app
from app.pipeline import Pipeline, PipelineConfig
from tests.fixtures import synthetic as S
from tests.test_retrieval_pipeline import Q, SUP, ScriptedGateway, cited_good


def client_for(script):
    def factory():
        cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 3, False))
        return Pipeline(S.retriever(), ScriptedGateway(script), cfg), "stand-in"
    return TestClient(create_app(factory))


def test_answer_200_with_source_label_and_citations():
    c = client_for([cited_good(), SUP])
    r = c.post("/ask", json={"question": Q})
    j = r.json()
    assert r.status_code == 200 and j["answered"] and j["source"] == "stand-in" and j["citations"]
    assert c.get("/stats").json()["answered"] == 1


def test_refusal_is_200_not_an_error():
    r = client_for([]).post("/ask", json={"question": "what is the pricing of bitcoin mining hardware"})
    assert r.status_code == 200 and r.json()["refused"] and r.json()["answer"] == ""


def test_provider_failures_map_to_honest_http_codes_and_service_stays_up():
    for status, code in (("rate_limited", 429), ("timeout", 504), ("error", 502)):
        c = client_for([GatewayResult(False, status, status, user_message=f"msg {status}", attempts=1),
                        GatewayResult(False, status, status, user_message="again", attempts=1)])
        r = c.post("/ask", json={"question": Q})
        assert r.status_code == code and r.json()["message"] == f"msg {status}" and not r.json()["answered"]
        assert c.get("/health").json()["ok"] is True        # still up after the failure
        assert c.post("/ask", json={"question": Q}).status_code == code


def test_invalid_question_is_400_and_missing_library_is_503_not_a_crash():
    assert client_for([]).post("/ask", json={"question": "   "}).status_code == 400

    def broken():
        raise FileNotFoundError("research/index/c300o50/chunks.jsonl missing. Run: python -m scripts.build_index")
    c = TestClient(create_app(broken))
    assert c.get("/health").json()["ok"] is False
    r = c.post("/ask", json={"question": Q})
    assert r.status_code == 503 and "build_index" in r.json()["message"]
