"""REST API (FastAPI). Run with `neo serve` or `uvicorn neo.api:app`.

Authentication: in production put this behind the API gateway (OIDC / SAML) and map the authenticated identity
to `X-User`; the header is trusted only from the gateway. Every route checks the user against config/users.yaml.

  POST /questions                 {"question"}          -> {"job_id"}       (asynchronous; workers run it)
  POST /questions/sync            {"question"}          -> the full result  (small questions, demos)
  GET  /jobs/{job_id}                                   -> job status and run id
  GET  /runs                                            -> the user's recent runs
  GET  /runs/{run_id}                                   -> status, answer, review state
  GET  /runs/{run_id}/certificate                       -> the full proof certificate (markdown)
  POST /runs/{run_id}/feedback    {"rating", "comment"}
  POST /runs/{run_id}/replay      {"latest"}
  GET  /reviews                                         -> pending reviews (approvers only)
  POST /reviews/{run_id}          {"approve", "note"}
  GET  /ontology/search?q=...                           -> linked ontology objects
  GET  /ontology/versions
  GET  /health, GET /stats
  GET  /                                                -> the web UI (step-by-step view of each question)
  GET  /ui/config, POST /ui/ask, GET /ui/runs/{run_id}/explain, GET /ui/ontology[/overview|/trace]  (web UI)
"""
from __future__ import annotations

import copy
import os
import threading
from contextlib import asynccontextmanager
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import ROOT
from .explain import explain, ontology_overview, ontology_trace, ontology_view
from .governance.identity import AccessError
from .llm.gateway import LLMGateway
from .llm.providers import LLMError, ScriptedLLM
from .orchestrator import Neo

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
GOLDEN = os.path.join(ROOT, "golden", "core.yaml")


class Question(BaseModel):
    question: str


class Feedback(BaseModel):
    rating: int
    comment: str = ""


class Decision(BaseModel):
    approve: bool
    note: str = ""


class Replay(BaseModel):
    latest: bool = False


class UIAsk(BaseModel):
    question: str
    model: Optional[dict] = None          # supplied model: the LLM steps are skipped, everything else runs


def llm_status(neo: Neo):
    """(ready, description) of the model that writes models; an Anthropic client without credentials is not ready."""
    try:
        llm = neo.gateway.llm("formalize")
    except LLMError as e:
        return False, f"unavailable ({e})"
    client = getattr(llm, "client", None)
    if llm.name == "anthropic" and client is not None and not (getattr(client, "api_key", None) or
                                                               getattr(client, "auth_token", None)):
        return False, f"{llm.describe()}: no credentials (set ANTHROPIC_API_KEY)"
    return True, llm.describe()


def supplied(neo: Neo, model: dict) -> Neo:
    """A copy of the platform whose LLM steps return a supplied model (as `neo eval` does in scripted mode)."""
    clone = copy.copy(neo)
    writer = ScriptedLLM(responses=[model, {"verdict": "ok", "note": "supplied model: no LLM review"}])
    note = "Supplied model: no language model was called, so there is no prose answer. The proved result is above."
    clone.gateway = LLMGateway(neo.cfg, neo.runs, providers={
        "default": writer, "formalize": writer, "review": writer, "answer": ScriptedLLM(responses=[note]),
        "requirements": ScriptedLLM(responses=[{"requirements": []}])})
    return clone


def create_app(neo: Optional[Neo] = None, workers: int = 0) -> FastAPI:
    state = {"neo": neo}
    stop = threading.Event()

    def platform() -> Neo:
        if state["neo"] is None:
            state["neo"] = Neo()
        return state["neo"]

    @asynccontextmanager
    async def lifespan(_app):
        for _ in range(workers):          # background workers run queued questions
            threading.Thread(target=platform().work, kwargs={"stop": stop}, daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="neo", version="0.1.0", lifespan=lifespan,
                  description="Ontology-grounded, proof-carrying answers over the data lake")

    def user(x_user: str = Header(..., alias="X-User")):
        try:
            return platform().principal(x_user)
        except AccessError as e:
            raise HTTPException(401, str(e))

    @app.get("/health")
    def health():
        n = platform()
        return {"ok": True, "ontology_version": n.store.current_version(), "snapshot": n.catalog.snapshot().id}

    @app.post("/questions")
    def submit(q: Question, p=Depends(user)):
        return {"job_id": platform().submit(q.question, p.id)}

    @app.post("/questions/sync")
    def ask(q: Question, p=Depends(user)):
        r = platform().ask(q.question, p.id)
        return r.as_dict()

    @app.get("/jobs/{job_id}")
    def job(job_id: str, p=Depends(user)):
        j = platform().runs.job(job_id)
        if not j or j["user"] != p.id:
            raise HTTPException(404, "no such job")
        return j

    def own_run(run_id: str, p):
        r = platform().runs.run(run_id)
        if not r or (r["user"] != p.id and not p.can_approve):
            raise HTTPException(404, "no such run")
        return r

    @app.get("/runs")
    def runs(p=Depends(user), limit: int = 50):
        return platform().runs.list_runs(limit, None if p.can_approve else p.id)

    @app.get("/runs/{run_id}")
    def run(run_id: str, p=Depends(user)):
        r = own_run(run_id, p)
        n = platform()
        return {**{k: r[k] for k in ("id", "question", "user", "status", "form", "answer_summary", "created",
                                     "seconds", "needs_review", "ontology_version")},
                "snapshot": r["snapshot"].get("id"), "answer": n.runs.read(run_id, "answer.md", as_json=False),
                "steps": n.runs.steps(run_id)}

    @app.get("/runs/{run_id}/certificate")
    def certificate(run_id: str, p=Depends(user)):
        own_run(run_id, p)
        return {"certificate": platform().runs.read(run_id, "certificate.md", as_json=False)}

    @app.post("/runs/{run_id}/feedback")
    def feedback(run_id: str, f: Feedback, p=Depends(user)):
        own_run(run_id, p)
        n = platform()
        n.runs.add_feedback(run_id, p.id, f.rating, f.comment)
        if f.rating < 0:
            n.runs.curate("negative_feedback", run_id, f.comment, run_id)
        return {"ok": True}

    @app.post("/runs/{run_id}/replay")
    def replay(run_id: str, body: Replay, p=Depends(user)):
        own_run(run_id, p)
        return platform().replay(run_id, latest=body.latest)

    @app.get("/reviews")
    def reviews(p=Depends(user)):
        if not p.can_approve:
            raise HTTPException(403, "your role cannot approve reviews")
        return platform().runs.reviews("pending")

    @app.post("/reviews/{run_id}")
    def decide(run_id: str, d: Decision, p=Depends(user)):
        if not p.can_approve:
            raise HTTPException(403, "your role cannot approve reviews")
        try:
            platform().runs.decide_review(run_id, p.id, d.approve, d.note)
        except KeyError as e:
            raise HTTPException(404, str(e))
        platform().audit.append("review_decided", p.id, run_id=run_id, approve=d.approve, note=d.note)
        return {"ok": True}

    @app.get("/ontology/search")
    def search(q: str, p=Depends(user)):
        n = platform()
        onto = n.ontology.load(None, n.catalog.snapshot())
        return [{"id": l.id, "kind": l.kind, "score": l.score, "matched": l.matched}
                for l in n.ontology.linker(onto).link(q)]

    @app.get("/ontology/versions")
    def versions(p=Depends(user)):
        return platform().store.list_versions()

    @app.get("/stats")
    def stats(p=Depends(user)):
        return platform().runs.stats()

    # ------------------------------------------------------------------ web UI
    @app.get("/", include_in_schema=False)
    def web():
        return FileResponse(WEB, media_type="text/html")

    @app.get("/ui/config")
    def ui_config():
        n = platform()
        users = []
        for uid in n.directory.users:
            p = n.principal(uid)
            users.append({"id": uid, "name": p.name, "roles": p.roles, "clearance": p.clearance,
                          "row_policies": p.row_policies})
        ready, llm = llm_status(n)
        cases = (yaml.safe_load(open(GOLDEN)) or {}).get("cases", []) if os.path.exists(GOLDEN) else []
        return {"users": users, "llm": llm, "llm_available": ready,
                "ontology_version": n.store.current_version(), "snapshot": n.catalog.snapshot().id,
                "examples": [{"id": c["id"], "question": c["question"], "user": c.get("user") or n.directory.default_user(),
                              "form": (c.get("model") or {}).get("form"), "model": c.get("model")} for c in cases
                             if (c.get("user") or n.directory.default_user()) in n.directory.users]}

    @app.post("/ui/ask")
    def ui_ask(q: UIAsk, p=Depends(user)):
        n = platform()
        runner = supplied(n, q.model) if q.model else n
        run_id = n.start(q.question, p.id)

        def go():
            try:
                runner.resume(run_id)
            except Exception as e:  # keep the run visible in the UI instead of leaving it "running"
                n.runs.update_run(run_id, status="error", state="finished", answer_summary=f"{type(e).__name__}: {e}")
        threading.Thread(target=go, daemon=True).start()
        return {"run_id": run_id}

    @app.get("/ui/runs/{run_id}/explain")
    def ui_explain(run_id: str, p=Depends(user)):
        own_run(run_id, p)
        return explain(platform(), run_id)

    @app.get("/ui/ontology")
    def ui_ontology(p=Depends(user)):
        return ontology_view(platform(), p)

    @app.get("/ui/ontology/overview")
    def ui_ontology_overview(p=Depends(user)):
        return ontology_overview(platform(), p)

    @app.get("/ui/ontology/trace")
    def ui_ontology_trace(q: str, p=Depends(user)):
        return ontology_trace(platform(), p, q)

    return app


app = create_app()
