"""The orchestrator: one durable workflow per question, wiring every service together.

  pin          snapshot of the lake, ontology version, rule versions (stored with the run)
  retrieve     the part of the ontology the question needs; clarification if nothing links
  requirements a second LLM, which never sees the model, lists what the question needs (verify/fidelity.py)
  formalize    LLM writes the model (or a plan of small models) -> validator -> prover -> fidelity check against
               the requirements; errors go back to the LLM (repair loop)
  review       LLM re-reads its model and the result; a revision is re-validated, re-proved and re-checked
  certify      certificate rendered by code (full, and an LLM-safe version without row data), plus the values the
               answer may cite
  answer       LLM writes the answer with numbers as slots; the answer check verifies every claim and sends
               failures back (verify/answer.py)
  ledger       one row per claim (question, model, answer) and one Lean run over the claim theorems
  route        human review queue when the answer is not a clean proof, a requirement is unmet, or high impact
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
from .ontology.service import OntologyService, owed_caveats
from .ontology.store import make_store
from .proof.certificate import render
from .proof.plan import node_models, prove_plan
from .proof.prover import ProofError, Prover
from .rules import RuleRegistry
from .runs import RunStore
from .semantic.validator import ValidationContext, validate
from .util import extract_json, now_iso
from .verify import ledger as claim_ledger
from .verify.answer import AnswerCheck, finalize, parse_reply
from .verify.fidelity import Fidelity, parse_requirements, summary as fidelity_summary
from .verify.values import catalog as value_catalog, llm_section

PROMPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
ROLE = """You turn business questions into formal models over the company's data, grounded in its ontology.
You never compute answers and never see raw data. You write a model; a validator checks it against the ontology,
the asker's access rights, units and approved rules; a compiler runs it as exact SQL inside the data lake; Z3 and an
independent recomputation prove the result."""
REQ_ROLE = """You read business questions and state precisely what a correct answer must respect, as small claims a
program can check. You never write the model that answers the question and never see data."""


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
            reqs = self._step(run_id, "requirements", tracer, lambda: self._requirements(
                question, principal, retrieval, run_id))
            system = "\n\n".join([ROLE, self.manual, "## Functions\n" + DOCS, retrieval["briefing"]])
            formal = self._step(run_id, "formalize", tracer, lambda: self._formalize(
                onto, question, principal, engine, snapshot, system, run_id, reqs["requirements"]))
            if formal.get("needs_approval"):
                raise _Stop("needs_approval", raw=formal["raw"], reasons=[f"needs approval: {formal['needs_approval']}"])
            if formal.get("failed"):
                raise _Stop("no_model", errors=formal["errors"], attempts=formal["attempts"])
            final = self._step(run_id, "review", tracer, lambda: self._review(
                onto, question, principal, engine, snapshot, system, formal, run_id, reqs["requirements"]))
            raw, proof = final["raw"], final["proof"]
            certs = self._step(run_id, "certify", tracer, lambda: self._certify(
                onto, question, principal, snapshot, raw, proof, run_id))
            answer = self._step(run_id, "answer", tracer, lambda: self._answer(question, principal, raw, proof,
                                                                                certs, run_id))
            ledger = self._step(run_id, "ledger", tracer, lambda: claim_ledger.build(reqs, proof, answer, self.cfg))
            reasons = self._step(run_id, "route", tracer, lambda: self._route(onto, question, principal, snapshot,
                                                                               raw, proof, run_id))
            status = proof["status"]
            cert = certs["full"] + ("\n\n" + ledger["markdown"] if ledger.get("markdown") else "")
            result = Result(run_id, question, user, status, answer["text"], cert, proof, raw,
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

    def _requirements(self, question, principal, retrieval, run_id) -> dict:
        if not self.cfg["pipeline"].get("requirements", True) or not self.gateway.routes("requirements"):
            return {"status": "skipped", "requirements": [],
                    "note": "the requirements writer is not configured (llm.tasks.requirements)"}
        system = "\n\n".join([REQ_ROLE, "## Functions\n" + DOCS, retrieval["briefing"]])
        try:
            reply = self.gateway.chat("requirements", system, [{"role": "user", "content": prompt(
                "requirements", QUESTION=question)}], principal, run_id)
        except LLMError as e:
            return {"status": "failed", "requirements": [], "note": f"the requirements writer failed: {e}"}
        reqs, problems = parse_requirements(extract_json(reply))
        status = "listed" if reqs else ("empty" if not problems else "failed")
        return {"status": status, "requirements": reqs, "problems": problems,
                "note": "; ".join(problems) or ("" if reqs else "the requirements writer listed nothing")}

    def _models(self, raw, onto, question, principal, snapshot):
        """Validated models: {"": model}, or {"N1": model, ...} for a plan. (models, errors, warnings, denied,
        needs_approval)."""
        ctx = self._ctx(onto, question, principal, snapshot)
        if isinstance(raw, dict) and raw.get("form") == "plan":
            errors: List[str] = []
            if not isinstance(raw.get("interpretation"), str) or len(raw["interpretation"].strip()) < 10:
                errors.append("`interpretation` must restate precisely, in a sentence, what is being asked.")
            nodes, shape = node_models(raw)
            errors += shape
            models, warnings, denied, approval = {}, [], [], None
            for nid, node in nodes.items():
                res = validate(node, ctx)
                errors += [f"node {nid}: {e}" for e in res.errors]
                warnings += [f"node {nid}: {w}" for w in res.warnings]
                denied += res.access_denied
                if res.model is not None:
                    models[nid] = res.model
                    approval = approval or res.model.needs_approval
            return models, errors, warnings, denied, approval
        res = validate(raw, ctx)
        return ({"": res.model} if res.model else {}), res.errors, res.warnings, res.access_denied, \
            (res.model.needs_approval if res.model else None)

    def _validate_and_prove(self, raw, onto, question, principal, engine, snapshot, run_id, requirements=None):
        """(proof, errors, needs_approval, models, fidelity errors)."""
        models, errors, warnings, denied, approval = self._models(raw, onto, question, principal, snapshot)
        for d in denied:
            self.audit.append("access_denied", principal.id, run_id=run_id, detail=d)
        if errors:
            for e in errors[:5]:
                self.runs.curate("validator_error", e.split(":")[0][:120], e, run_id)
            return None, errors, None, None, []
        single = models.get("")
        if single is not None and single.form == "not_expressible":
            return {"status": "not_expressible", "form": "not_expressible", "answer": None,
                    "answer_summary": single.interpretation, "verification": [], "self_check": {"matches": None},
                    "evidence": {}}, [], None, models, []
        if approval:
            return None, [], approval, models, []
        try:
            if single is not None:
                provers = {"": Prover(onto, single, engine, self.cfg, self.rules)}
                proof = provers[""].run()
            else:
                provers = {nid: Prover(onto, m, engine, self.cfg, self.rules, nested=True) for nid, m in models.items()}
                proof = prove_plan(raw, provers, self.cfg)
        except ProofError as e:
            return None, [f"The model could not be executed or proved: {e}"], None, models, []
        proof["warnings"] = warnings
        fid_errors: List[str] = []
        if requirements:
            fid = Fidelity(onto, requirements, self.cfg)
            results = []
            for nid, m in models.items():
                r, e = fid.run(m, provers[nid], node=nid, plan_node=bool(nid))
                results += r
                fid_errors += e
            proof["fidelity"] = {"results": results, "status": fidelity_summary(results),
                                 "lean_goals": fid.goals.items, "skipped": fid.goals.skipped}
        return proof, [], None, models, fid_errors

    @staticmethod
    def formalize_prompt(question: str, principal: Principal) -> str:
        pol = "".join(f"\nRow policy on `{e}`: {p}" for e, p in principal.row_policies.items())
        return prompt("formalize", QUESTION=question, USER=principal.name, CLEARANCE=principal.clearance,
                      POLICIES=(" — rows are limited by policy:" + pol) if pol else "")

    def _formalize(self, onto, question, principal, engine, snapshot, system, run_id, requirements=None) -> dict:
        messages = [{"role": "user", "content": self.formalize_prompt(question, principal)}]
        errors: List[str] = []
        max_attempts = self.cfg["pipeline"]["max_repairs"] + 1
        for n in range(1, max_attempts + 1):
            reply = self.gateway.chat("formalize", system, messages, principal, run_id)
            raw = extract_json(reply)
            fid_errors: List[str] = []
            if raw is None:
                proof, errors, approval = None, ["The reply did not contain a JSON object. Reply with only the model."], None
            else:
                proof, errors, approval, _, fid_errors = self._validate_and_prove(
                    raw, onto, question, principal, engine, snapshot, run_id, requirements)
            if proof is not None and fid_errors and n < max_attempts:
                self.runs.write(run_id, f"attempts/attempt_{n}.json", {"model": raw, "errors": fid_errors,
                                                                        "fidelity": proof.get("fidelity")},
                                overwrite=True)
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": prompt("fidelity_repair",
                                                                ERRORS="\n".join(f"- {e}" for e in fid_errors))}]
                continue
            self.runs.write(run_id, f"attempts/attempt_{n}.json", {"model": raw, "errors": errors,
                                                                    "fidelity": (proof or {}).get("fidelity")},
                            overwrite=True)
            if approval:
                return {"raw": raw, "needs_approval": approval, "attempts": n}
            if proof is not None:
                return {"raw": raw, "proof": proof, "attempts": n, "fidelity_errors": fid_errors,
                        "messages": messages + [{"role": "assistant", "content": reply}]}
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": prompt("repair", ERRORS="\n".join(f"- {e}" for e in errors))}]
        return {"failed": True, "errors": errors, "attempts": max_attempts}

    def _review(self, onto, question, principal, engine, snapshot, system, formal, run_id, requirements=None) -> dict:
        raw, proof = formal["raw"], formal["proof"]
        if not self.cfg["pipeline"].get("review", True) or proof["status"] == "not_expressible":
            return {"raw": raw, "proof": proof, "verdict": "skipped"}
        summary = json.dumps({"model": raw, "engine_result": self._compact(proof)}, indent=1, default=str)
        reply = self.gateway.chat("review", system, formal["messages"] + [
            {"role": "user", "content": prompt("review", QUESTION=question, SUMMARY=summary)}], principal, run_id)
        verdict = extract_json(reply) or {"verdict": "ok", "note": "review reply was not JSON; kept the model"}
        out = {"raw": raw, "proof": proof, "verdict": verdict.get("verdict", "ok"), "issues": verdict.get("issues", [])}
        if out["verdict"] == "revise" and isinstance(verdict.get("model"), dict):
            p2, errors, approval, _, fid_errors = self._validate_and_prove(
                verdict["model"], onto, question, principal, engine, snapshot, run_id, requirements)
            if p2 is not None and (not fid_errors or formal.get("fidelity_errors")):
                out.update(raw=verdict["model"], proof=p2, applied=True)
            elif p2 is not None:
                out.update(applied=False, reason=["the revision does not meet the requirements"] + fid_errors[:4])
            else:
                out.update(applied=False, reason=errors[:5] or [f"needs approval: {approval}"])
        return out

    def _certify(self, onto, question, principal, snapshot, raw, proof, run_id) -> dict:
        models, *_ = self._models(raw, onto, question, principal, snapshot)
        joins, policies, entities, used = {}, {}, set(), []
        for m in models.values():
            if m and m.universe:
                joins.update({j.alias: j.cardinality for j in m.universe.joins})
                ents = set(m.universe.aliases.values())
                entities |= ents
                policies.update({e: principal.row_policies[e] for e in ents if e in principal.row_policies})
                used += [f for _, f in m.used_fields]
        cited = {c["source"][5:] for c in raw.get("constraints") or [] if str(c.get("source", "")).startswith("rule:")}
        prov = {"snapshot": {"id": snapshot.id, "tables": snapshot.as_dict()}, "ontology_version": onto.version,
                "rules": {k: v for k, v in self.rules.pinned().items() if k in cited}, "user": principal.id,
                "llm": self.gateway.describe("formalize"), "run_id": run_id, "joins": joins, "row_policies": policies}
        single = models.get("")
        notes = single.notes if single else {}
        plan_units = {nid: ((m.notes or {}).get("target_units") or {}).get("value") for nid, m in models.items() if nid}
        values = value_catalog(proof, notes, plan_units)
        allow = self.cfg["governance"].get("allow_row_samples_to_llm", False)
        return {"full": render(question, raw, proof, prov, notes),
                "llm": render(question, raw, proof, prov, notes, for_llm=True, allow_rows=allow),
                "values": values, "caveats": owed_caveats(onto, question, used, entities),
                "entities": sorted(entities)}

    def _answer(self, question, principal, raw, proof, certs, run_id) -> dict:
        values, owed = certs.get("values") or {}, certs.get("caveats") or []
        caveats = ("Caveats this answer must mention (list each as a caveat claim):\n" +
                   "\n".join(f"- {o['id']}: {o['description']}" for o in owed)) if owed else ""
        messages = [{"role": "user", "content": prompt("answer", QUESTION=question, CERTIFICATE=certs["llm"],
                                                       VALUES=llm_section(values), CAVEATS=caveats)}]
        reply = self.gateway.chat("answer", ROLE, messages, principal, run_id)
        if not self.cfg["pipeline"].get("answer_check", True):
            return {"text": reply, "checked": False}
        context = json.dumps(raw, default=str) + " " + str(raw.get("interpretation", ""))
        checker = AnswerCheck(values, question, context, certs["llm"] + "\n" + llm_section(values),
                              (proof.get("fidelity") or {}).get("results"), owed, self.cfg)
        report = checker.check(parse_reply(reply))
        drafts = [{"text": report["text"], "problems": report["problems"]}]
        for _ in range(int(self.cfg["pipeline"].get("answer_repairs", 2))):
            if not report["problems"]:
                break
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": prompt("answer_repair", PROBLEMS="\n".join(
                             f"- {x}" for x in report["problems"]))}]
            try:
                reply = self.gateway.chat("answer", ROLE, messages, principal, run_id)
            except LLMError as e:
                drafts.append({"text": "", "problems": [f"the answer writer failed: {e}"]})
                break
            report = checker.check(parse_reply(reply))
            drafts.append({"text": report["text"], "problems": report["problems"]})
        final = finalize(report, {o["id"]: o for o in owed})
        return {"text": final["text"], "checked": True, "format": final["format"], "claims": final["claims"],
                "lean_goals": final["lean_goals"], "drafts": drafts, "removed": final["removed"],
                "added_caveats": final["added_caveats"]}

    def _route(self, onto, question, principal, snapshot, raw, proof, run_id) -> dict:
        certs = self.runs.load_step(run_id, "certify") or {}
        reasons = review_reasons(self.cfg, proof, onto, raw, certs.get("entities", []))
        reasons += claim_ledger.review_reasons(self.runs.load_step(run_id, "ledger") or {})
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
        models, errors, *_ = self._models(raw, onto, run["question"], principal, snapshot)
        if errors:
            return {"run_id": run_id, "reproduced": False, "errors": errors}
        engine = self.engine.bind(snapshot)
        if "" in models and models[""].form == "not_expressible":
            return {"run_id": run_id, "reproduced": True, "status": "not_expressible"}
        if "" in models:
            proof = Prover(onto, models[""], engine, self.cfg, self.rules).run()
        else:
            proof = prove_plan(raw, {nid: Prover(onto, m, engine, self.cfg, self.rules, nested=True)
                                     for nid, m in models.items()}, self.cfg)
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
