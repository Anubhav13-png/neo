"""The orchestrator: one durable workflow per question, wiring every service together.

  pin        snapshot of the lake, ontology version, rule versions (stored with the run)
  retrieve   the part of the ontology the question needs; clarification if nothing links
  formalize  LLM writes the model -> validator -> prover; errors go back to the LLM (repair loop)
  review     LLM re-reads its model and the result; a revision is re-validated and re-proved
  certify    certificate rendered by code (full, and an LLM-safe version without row data)
  answer     LLM writes the answer from the LLM-safe certificate only
  route      human review queue when the answer is not a clean proof or is high impact
Every step is checkpointed in the run store, so `resume(run_id)` continues after a crash, and `replay(run_id)`
re-proves a stored model on its pinned snapshot without any LLM.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import ROOT, load_config
from .dsl import DOCS
from .governance.audit import AuditLog
from .governance.identity import AccessError, Directory, Principal
from .governance.review import review_reasons
from .lake.catalog import LocalLakehouse, Snapshot
from .lake.engines import make_engine
from .llm.gateway import LLMGateway
from .llm.providers import LLMError
from .observability import Tracer
from .ontology.service import OntologyService
from .ontology.store import make_store
from .proof.certificate import render
from .proof.prover import ProofError, Prover
from .rules import RuleRegistry
from .runs import RunStore
from .semantic.validator import ValidationContext, validate
from .util import extract_json, now_iso

PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
ROLE = """You turn business questions into formal models over the company's data, grounded in its ontology.
You never compute answers and never see raw data. You write a model; a validator checks it against the ontology,
the asker's access rights, units and approved rules; a compiler runs it as exact SQL inside the data lake; Z3 and an
independent recomputation prove the result."""


def prompt(name: str, **values) -> str:
    text = open(os.path.join(PROMPTS, f"{name}.md")).read()
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


@dataclass
class Result:
    run_id: str
    question: str
    user: str
    status: str
    answer: str = ""
    certificate: str = ""
    proof: Optional[dict] = None
    model: Optional[dict] = None
    review_reasons: List[str] = field(default_factory=list)
    clarification: str = ""
    attempts: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


class _Stop(Exception):
    def __init__(self, status: str, **info):
        self.status, self.info = status, info


class Neo:
    def __init__(self, cfg: Optional[dict] = None, gateway: Optional[LLMGateway] = None, catalog=None):
        self.cfg = cfg or load_config()
        c = self.cfg
        if catalog is None:
            if c["lake"].get("kind", "local") != "local":
                raise NotImplementedError("for lake.kind sqlalchemy pass a Catalog implementation for your lake "
                                          "(Iceberg REST, Glue, Unity) as `catalog`")
            catalog = LocalLakehouse(c["lake"]["root"])
        self.catalog = catalog
        self.engine = make_engine(c, catalog)
        self.store = make_store(c)
        self.ontology = OntologyService(self.store, catalog)
        self.rules = RuleRegistry(c["rules_dir"])
        self.directory = Directory(c["governance"]["users"])
        self.audit = AuditLog(c["governance"]["audit_log"])
        self.runs = RunStore(c["runs"]["root"])
        self.gateway = gateway or LLMGateway(c, self.runs)
        self.manual = open(os.path.join(PROMPTS, "manual.md")).read()

    # ------------------------------------------------------------------ helpers
    def principal(self, user: str) -> Principal:
        return self.directory.principal(user)

    def _ctx(self, onto, question, principal, snapshot) -> ValidationContext:
        return ValidationContext(onto, question, principal, self.rules, self.catalog, snapshot, self.cfg)

    def _step(self, run_id: str, name: str, tracer: Tracer, fn: Callable[[], Any]):
        if self.runs.step_done(run_id, name):
            return self.runs.load_step(run_id, name)
        started, t0 = now_iso(), time.time()
        with tracer.span(name):
            out = fn()
        self.runs.save_step(run_id, name, out, started, round((time.time() - t0) * 1000, 1))
        self.runs.update_run(run_id, state=name)
        return out

    def _pinned(self, run: dict):
        snapshot = Snapshot.from_dict(run["snapshot"]["tables"])
        return snapshot, self.engine.bind(snapshot), self.ontology.load(run["ontology_version"], snapshot)

    @staticmethod
    def _compact(proof: dict) -> dict:
        keep = ("form", "status", "answer_summary", "answer", "exact_values", "eligible_rows", "universe_rows",
                "parameters", "skipped_missing", "verification", "self_check", "solver", "backtest", "sensitivity",
                "exactness")
        out = {k: proof.get(k) for k in keep if k in proof}
        if out.get("solver"):
            out["solver"] = {k: v for k, v in out["solver"].items() if k != "probes"}
        text = json.dumps(out, default=str)
        if len(text) > 12000 and isinstance(out.get("answer"), (list, dict)):
            out["answer"] = json.dumps(out["answer"], default=str)[:6000] + " ...(truncated)"
        return out

    # ------------------------------------------------------------------ the workflow
    def ask(self, question: str, user: str, ontology_version: Optional[str] = None) -> Result:
        return self.resume(self.start(question, user, ontology_version))

    def start(self, question: str, user: str, ontology_version: Optional[str] = None) -> str:
        """Pin the snapshot, ontology version and rules for a new run; `resume` then runs it."""
        self.principal(user)
        snapshot = self.catalog.snapshot()
        version = ontology_version or self.store.current_version()
        run_id = self.runs.create_run(question, user, version,
                                      {"id": snapshot.id, "tables": snapshot.as_dict()}, self.rules.pinned())
        self.audit.append("question_asked", user, run_id=run_id, question=question, snapshot=snapshot.id,
                          ontology_version=version)
        return run_id

    def resume(self, run_id: str) -> Result:
        t0 = time.time()
        run = self.runs.run(run_id)
        if run is None:
            raise KeyError(run_id)
        question, user = run["question"], run["user"]
        principal = self.principal(user)
        snapshot, engine, onto = self._pinned(run)
        tracer = Tracer(run_id)
        try:
            retrieval = self._step(run_id, "retrieve", tracer, lambda: self._retrieve(onto, question, principal,
                                                                                      engine, snapshot, run_id))
            if retrieval["needs_clarification"]:
                raise _Stop("needs_clarification", clarification=retrieval["clarification"])
            system = "\n\n".join([ROLE, self.manual, "## Functions\n" + DOCS, retrieval["briefing"]])
            formal = self._step(run_id, "formalize", tracer, lambda: self._formalize(
                onto, question, principal, engine, snapshot, system, run_id))
            if formal.get("needs_approval"):
                raise _Stop("needs_approval", raw=formal["raw"], reasons=[f"needs approval: {formal['needs_approval']}"])
            if formal.get("failed"):
                raise _Stop("no_model", errors=formal["errors"], attempts=formal["attempts"])
            final = self._step(run_id, "review", tracer, lambda: self._review(
                onto, question, principal, engine, snapshot, system, formal, run_id))
            raw, proof = final["raw"], final["proof"]
            certs = self._step(run_id, "certify", tracer, lambda: self._certify(
                onto, question, principal, snapshot, raw, proof, run_id))
            answer = self._step(run_id, "answer", tracer, lambda: self._answer(question, principal, raw, certs, run_id))
            reasons = self._step(run_id, "route", tracer, lambda: self._route(onto, question, principal, snapshot,
                                                                               raw, proof, run_id))
            status = proof["status"]
            result = Result(run_id, question, user, status, answer["text"], certs["full"], proof, raw,
                            reasons["reasons"], attempts=formal["attempts"])
        except _Stop as stop:
            info = stop.info
            if stop.status == "needs_approval":
                self.runs.enqueue_review(run_id, info["reasons"])
            text = {"needs_clarification": info.get("clarification", ""),
                    "needs_approval": "This question needs approval before it runs: " + "; ".join(info.get("reasons", [])),
                    "no_model": "Could not build a valid model, so no answer is given. Last problems:\n" +
                                "\n".join(f"- {e}" for e in info.get("errors", [])[:8])}[stop.status]
            result = Result(run_id, question, user, stop.status, text, model=info.get("raw"),
                            review_reasons=info.get("reasons", []), clarification=info.get("clarification", ""),
                            attempts=info.get("attempts", 0))
        except LLMError as e:
            result = Result(run_id, question, user, "llm_error", f"The language model failed: {e}")
        result.seconds = round(time.time() - t0, 2)
        self._finish(result, tracer, snapshot)
        return result

    def _retrieve(self, onto, question, principal, engine, snapshot, run_id) -> dict:
        r = self.ontology.retrieve(onto, question, principal.clearance, engine, snapshot, self.rules.all(), self.cfg,
                                   restricted=set(principal.row_policies))
        for w in r.unlinked_words:
            self.runs.curate("unlinked_word", w, question, run_id)
        if r.needs_clarification:
            self.runs.curate("clarification", question[:200], r.clarification, run_id)
        return {"needs_clarification": r.needs_clarification, "clarification": r.clarification,
                "summary": r.summary(), "briefing": "" if r.needs_clarification else OntologyService.render(onto, r)}

    def _validate_and_prove(self, raw, onto, question, principal, engine, snapshot, run_id):
        """(proof, errors, needs_approval, model)."""
        res = validate(raw, self._ctx(onto, question, principal, snapshot))
        for d in res.access_denied:
            self.audit.append("access_denied", principal.id, run_id=run_id, detail=d)
        if res.errors:
            for e in res.errors[:5]:
                self.runs.curate("validator_error", e.split(":")[0][:120], e, run_id)
            return None, res.errors, None, None
        model = res.model
        if model.form == "not_expressible":
            return {"status": "not_expressible", "form": "not_expressible", "answer": None,
                    "answer_summary": model.interpretation, "verification": [], "self_check": {"matches": None},
                    "evidence": {}}, [], None, model
        if model.needs_approval:
            return None, [], model.needs_approval, model
        try:
            proof = Prover(onto, model, engine, self.cfg, self.rules).run()
        except ProofError as e:
            return None, [f"The model could not be executed or proved: {e}"], None, model
        proof["warnings"] = res.warnings
        return proof, [], None, model

    @staticmethod
    def formalize_prompt(question: str, principal: Principal) -> str:
        pol = "".join(f"\nRow policy on `{e}`: {p}" for e, p in principal.row_policies.items())
        return prompt("formalize", QUESTION=question, USER=principal.name, CLEARANCE=principal.clearance,
                      POLICIES=(" — rows are limited by policy:" + pol) if pol else "")

    def _formalize(self, onto, question, principal, engine, snapshot, system, run_id) -> dict:
        messages = [{"role": "user", "content": self.formalize_prompt(question, principal)}]
        errors: List[str] = []
        max_attempts = self.cfg["pipeline"]["max_repairs"] + 1
        for n in range(1, max_attempts + 1):
            reply = self.gateway.chat("formalize", system, messages, principal, run_id)
            raw = extract_json(reply)
            if raw is None:
                proof, errors, approval = None, ["The reply did not contain a JSON object. Reply with only the model."], None
            else:
                proof, errors, approval, _ = self._validate_and_prove(raw, onto, question, principal, engine,
                                                                      snapshot, run_id)
            self.runs.write(run_id, f"attempts/attempt_{n}.json", {"model": raw, "errors": errors}, overwrite=True)
            if approval:
                return {"raw": raw, "needs_approval": approval, "attempts": n}
            if proof is not None:
                return {"raw": raw, "proof": proof, "attempts": n, "messages": messages + [{"role": "assistant",
                                                                                            "content": reply}]}
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": prompt("repair", ERRORS="\n".join(f"- {e}" for e in errors))}]
        return {"failed": True, "errors": errors, "attempts": max_attempts}

    def _review(self, onto, question, principal, engine, snapshot, system, formal, run_id) -> dict:
        raw, proof = formal["raw"], formal["proof"]
        if not self.cfg["pipeline"].get("review", True) or proof["status"] == "not_expressible":
            return {"raw": raw, "proof": proof, "verdict": "skipped"}
        summary = json.dumps({"model": raw, "engine_result": self._compact(proof)}, indent=1, default=str)
        reply = self.gateway.chat("review", system, formal["messages"] + [
            {"role": "user", "content": prompt("review", QUESTION=question, SUMMARY=summary)}], principal, run_id)
        verdict = extract_json(reply) or {"verdict": "ok", "note": "review reply was not JSON; kept the model"}
        out = {"raw": raw, "proof": proof, "verdict": verdict.get("verdict", "ok"), "issues": verdict.get("issues", [])}
        if out["verdict"] == "revise" and isinstance(verdict.get("model"), dict):
            p2, errors, approval, _ = self._validate_and_prove(verdict["model"], onto, question, principal, engine,
                                                               snapshot, run_id)
            if p2 is not None:
                out.update(raw=verdict["model"], proof=p2, applied=True)
            else:
                out.update(applied=False, reason=errors[:5] or [f"needs approval: {approval}"])
        return out

    def _certify(self, onto, question, principal, snapshot, raw, proof, run_id) -> dict:
        res = validate(raw, self._ctx(onto, question, principal, snapshot))
        model = res.model
        joins, policies = {}, {}
        if model and model.universe:
            joins = {j.alias: j.cardinality for j in model.universe.joins}
            policies = {e: principal.row_policies[e] for e in set(model.universe.aliases.values())
                        if e in principal.row_policies}
        cited = {c["source"][5:] for c in raw.get("constraints") or [] if str(c.get("source", "")).startswith("rule:")}
        prov = {"snapshot": {"id": snapshot.id, "tables": snapshot.as_dict()}, "ontology_version": onto.version,
                "rules": {k: v for k, v in self.rules.pinned().items() if k in cited}, "user": principal.id,
                "llm": self.gateway.describe("formalize"), "run_id": run_id, "joins": joins, "row_policies": policies}
        notes = model.notes if model else {}
        allow = self.cfg["governance"].get("allow_row_samples_to_llm", False)
        return {"full": render(question, raw, proof, prov, notes),
                "llm": render(question, raw, proof, prov, notes, for_llm=True, allow_rows=allow),
                "entities": sorted(set(model.universe.aliases.values())) if model and model.universe else []}

    def _answer(self, question, principal, raw, certs, run_id) -> dict:
        text = self.gateway.chat("answer", ROLE, [{"role": "user", "content": prompt(
            "answer", QUESTION=question, CERTIFICATE=certs["llm"])}], principal, run_id)
        return {"text": text}

    def _route(self, onto, question, principal, snapshot, raw, proof, run_id) -> dict:
        certs = self.runs.load_step(run_id, "certify") or {}
        reasons = review_reasons(self.cfg, proof, onto, raw, certs.get("entities", []))
        if reasons:
            self.runs.enqueue_review(run_id, reasons)
        return {"reasons": reasons}

    def _finish(self, result: Result, tracer: Tracer, snapshot):
        rid = result.run_id
        if result.model:
            self.runs.write(rid, "model.json", result.model, overwrite=True)
        if result.proof:
            self.runs.write(rid, "proof.json", result.proof, overwrite=True)
        if result.certificate:
            self.runs.write(rid, "certificate.md", result.certificate, overwrite=True)
        self.runs.write(rid, "answer.md", f"# {result.question}\n\n{result.answer}\n\n{result.certificate}\n",
                        overwrite=True)
        self.runs.write(rid, "trace.json", tracer.export(), overwrite=True)
        self.runs.update_run(rid, status=result.status, state="finished", form=(result.proof or {}).get("form"),
                             answer_summary=(result.proof or {}).get("answer_summary", "")[:500] if result.proof else None,
                             seconds=result.seconds, llm=self.gateway.describe("formalize"))
        self.audit.append("answer_delivered", result.user, run_id=rid, status=result.status, snapshot=snapshot.id,
                          entities=(self.runs.load_step(rid, "certify") or {}).get("entities", []),
                          needs_review=bool(result.review_reasons))

    # ------------------------------------------------------------------ replay, queue
    def replay(self, run_id: str, latest: bool = False) -> dict:
        run = self.runs.run(run_id)
        raw, old = self.runs.read(run_id, "model.json"), self.runs.read(run_id, "proof.json")
        if run is None or raw is None:
            raise KeyError(f"run {run_id} has no stored model")
        principal = self.principal(run["user"])
        if latest:
            snapshot = self.catalog.snapshot()
            version = self.store.current_version()
        else:
            snapshot = Snapshot.from_dict(run["snapshot"]["tables"])
            version = run["ontology_version"]
        onto = self.ontology.load(version, snapshot)
        res = validate(raw, self._ctx(onto, run["question"], principal, snapshot))
        if res.errors:
            return {"run_id": run_id, "reproduced": False, "errors": res.errors}
        if res.model.form == "not_expressible":
            return {"run_id": run_id, "reproduced": True, "status": "not_expressible"}
        proof = Prover(onto, res.model, self.engine.bind(snapshot), self.cfg, self.rules).run()
        same = json.dumps(proof.get("answer"), sort_keys=True, default=str) == \
            json.dumps((old or {}).get("answer"), sort_keys=True, default=str)
        changed_rules = [k for k, v in run["rules"].items() if self.rules.pinned().get(k, {}).get("hash") != v.get("hash")]
        self.audit.append("replayed", "system", run_id=run_id, latest=latest, same_answer=same)
        return {"run_id": run_id, "snapshot": snapshot.id, "ontology_version": version, "latest": latest,
                "status": proof["status"], "answer": proof.get("answer"), "original_answer": (old or {}).get("answer"),
                "same_answer": same, "rules_changed_since": changed_rules}

    def submit(self, question: str, user: str) -> str:
        self.principal(user)
        return self.runs.submit_job(question, user)

    def work(self, stop: Optional[threading.Event] = None, idle_seconds: float = 1.0, max_jobs: Optional[int] = None):
        """Worker loop: claim queued questions and run them. Run several workers for throughput."""
        done = 0
        while not (stop and stop.is_set()):
            job = self.runs.claim_job()
            if job is None:
                if max_jobs is not None:
                    return done
                time.sleep(idle_seconds)
                continue
            try:
                r = self.ask(job["question"], job["user"])
                self.runs.finish_job(job["id"], r.run_id)
            except Exception as e:  # a failed job must not stop the worker
                self.runs.finish_job(job["id"], None, f"{type(e).__name__}: {e}")
            done += 1
            if max_jobs is not None and done >= max_jobs:
                return done
        return done
