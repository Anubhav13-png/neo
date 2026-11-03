"""neo command line.

  neo init [--deals-csv PATH] [--fdic-cache DIR]   load the deals CSV and FDIC history, publish the ontology
  neo ask "question" [--user ID]            answer with a proof certificate (default: the first user in
                                            config/users.yaml)
  neo submit "question" [--user ID]         queue it; `neo worker` runs queued questions
  neo runs | show RUN | replay RUN [--latest] | resume RUN (continue an interrupted run from its checkpoints)
  neo review list | approve RUN | reject RUN --user raj
  neo feedback RUN --rating -1 --comment "..." --user demo
  neo ontology lint | publish [--eval golden/core.yaml] | versions | search "words" | briefing "question"
  neo ingest csv TABLE PATH | fdic SOURCE...
  neo eval golden/core.yaml [--live]
  neo lean build | status                   build and audit the Lean theorem library (lean/Neo)
  neo curation | audit | stats | serve [--port 8080 --workers 2]
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings

warnings.filterwarnings("ignore")


def _neo(args):
    from .config import load_config
    from .orchestrator import Neo
    cfg = load_config(args.config)
    if getattr(args, "provider", None):
        cfg["llm"]["default"] = {"provider": args.provider, **({"model": args.model} if args.model else {})}
        cfg["llm"]["tasks"] = {}
    return Neo(cfg)


def main(argv=None):
    p = argparse.ArgumentParser(prog="neo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--deals-csv", default=None, help="default: jp_morgan.csv next to the project folder")
    s.add_argument("--fdic-cache", default=None, help="saved FDIC API responses (default: the lake's cache)")

    s = sub.add_parser("ask")
    s.add_argument("question", nargs="+")
    s.add_argument("--user", default=None)
    s.add_argument("--provider")
    s.add_argument("--model")
    s.add_argument("--show-model", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-certificate", action="store_true")

    s = sub.add_parser("submit")
    s.add_argument("question", nargs="+")
    s.add_argument("--user", default=None)
    s = sub.add_parser("worker")
    s.add_argument("--once", action="store_true")
    s = sub.add_parser("job")
    s.add_argument("job_id")

    sub.add_parser("runs")
    s = sub.add_parser("show")
    s.add_argument("run_id")
    s = sub.add_parser("replay")
    s.add_argument("run_id")
    s.add_argument("--latest", action="store_true")
    s = sub.add_parser("resume")
    s.add_argument("run_id")

    s = sub.add_parser("review")
    s.add_argument("action", choices=["list", "approve", "reject"])
    s.add_argument("run_id", nargs="?")
    s.add_argument("--user", default="raj")
    s.add_argument("--note", default="")

    s = sub.add_parser("feedback")
    s.add_argument("run_id")
    s.add_argument("--rating", type=int, required=True)
    s.add_argument("--comment", default="")
    s.add_argument("--user", default=None)

    s = sub.add_parser("ontology")
    s.add_argument("action", choices=["lint", "publish", "versions", "search", "briefing"])
    s.add_argument("text", nargs="*")
    s.add_argument("--eval")
    s.add_argument("--note", default="")
    s.add_argument("--user", default=None)

    s = sub.add_parser("ingest")
    s.add_argument("kind", choices=["csv", "fdic"])
    s.add_argument("args", nargs="*")

    s = sub.add_parser("lean")
    s.add_argument("action", choices=["build", "status"])

    s = sub.add_parser("eval")
    s.add_argument("path")
    s.add_argument("--live", action="store_true")

    sub.add_parser("curation")
    sub.add_parser("audit")
    sub.add_parser("stats")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--workers", type=int, default=2)

    a = p.parse_args(argv)
    neo = _neo(a)
    if getattr(a, "user", "") is None:
        a.user = neo.directory.default_user()
    out = lambda o: print(json.dumps(o, indent=1, default=str))

    if a.cmd == "init":
        from .bootstrap import init_demo
        init_demo(neo, deals_csv=a.deals_csv, fdic_cache=a.fdic_cache)
    elif a.cmd == "ask":
        r = neo.ask(" ".join(a.question), a.user)
        if a.json:
            return out(r.as_dict())
        print("\n" + (r.answer or r.clarification).strip() + "\n")
        if a.show_model and r.model:
            print("----- model -----\n" + json.dumps(r.model, indent=1) + "\n")
        if r.certificate and not a.no_certificate:
            print(r.certificate + "\n")
        if r.review_reasons:
            print("[sent for human review: " + "; ".join(r.review_reasons) + "]")
        print(f"[status: {r.status} · attempts: {r.attempts} · {r.seconds}s · run {r.run_id}]")
    elif a.cmd == "submit":
        print(neo.submit(" ".join(a.question), a.user))
    elif a.cmd == "worker":
        n = neo.work(max_jobs=None if not a.once else 1)
        print(f"processed {n} job(s)")
    elif a.cmd == "job":
        out(neo.runs.job(a.job_id))
    elif a.cmd == "runs":
        for r in neo.runs.list_runs():
            print(f"{r['id']}  {r['status']:<20} {r['user']:<6} {r['question'][:70]}")
    elif a.cmd == "show":
        print(neo.runs.read(a.run_id, "answer.md", as_json=False) or "no such run")
    elif a.cmd == "replay":
        out(neo.replay(a.run_id, latest=a.latest))
    elif a.cmd == "resume":
        r = neo.resume(a.run_id)
        print("\n" + (r.answer or r.clarification).strip() + "\n")
        print(f"[status: {r.status} · run {r.run_id}]")
    elif a.cmd == "review":
        if a.action == "list":
            for r in neo.runs.reviews("pending"):
                print(f"{r['run_id']}  {r['run_status']:<14} {r['question'][:60]}\n    reasons: {'; '.join(r['reasons'])}")
        else:
            pr = neo.principal(a.user)
            if not pr.can_approve:
                sys.exit(f"{a.user} cannot approve reviews")
            neo.runs.decide_review(a.run_id, pr.id, a.action == "approve", a.note)
            neo.audit.append("review_decided", pr.id, run_id=a.run_id, approve=a.action == "approve", note=a.note)
            print("recorded")
    elif a.cmd == "feedback":
        neo.runs.add_feedback(a.run_id, a.user, a.rating, a.comment)
        if a.rating < 0:
            neo.runs.curate("negative_feedback", a.run_id, a.comment, a.run_id)
        print("recorded")
    elif a.cmd == "ontology":
        from .evaluation import publish_ontology
        from .ontology.lint import lint
        if a.action == "lint":
            errors, warns = lint(neo.store.source_document(), neo.catalog, neo.catalog.snapshot())
            out({"errors": errors, "warnings": warns})
        elif a.action == "publish":
            out(publish_ontology(neo, a.note, a.eval))
        elif a.action == "versions":
            out(neo.store.list_versions())
        else:
            snap = neo.catalog.snapshot()
            onto = neo.ontology.load(None, snap)
            text = " ".join(a.text)
            if a.action == "search":
                for l in neo.ontology.linker(onto).link(text):
                    print(f"{l.score:.2f}  {l.kind:<12} {l.id:<40} ({l.matched})")
            else:
                pr = neo.principal(a.user)
                r = neo.ontology.retrieve(onto, text, pr.clearance, neo.engine.bind(snap), snap, neo.rules.all(), neo.cfg)
                print(r.clarification if r.needs_clarification else neo.ontology.render(onto, r))
    elif a.cmd == "lean":
        from .proof import lean
        if a.action == "build":
            r = lean.build(neo.cfg)
            if not r["ok"]:
                print(r["output"][-4000:])
            print(f"{'ok' if r['ok'] else 'FAILED'}: {r.get('version', '')}, {len(r['axioms'])} theorems")
            for name, ax in r["axioms"].items():
                print(f"  {name:<40} axioms: {', '.join(ax) if ax else 'none'}")
            if not r["ok"]:
                sys.exit(1)
        else:
            tc = lean.toolchain(dict(neo.cfg, lean=dict(neo.cfg["lean"], enabled="auto")))
            out({"enabled": neo.cfg["lean"].get("enabled"), "installed": tc is not None,
                 "lean": (tc or {}).get("lean"), "version": lean.version(tc) if tc else None,
                 "project": neo.cfg["lean"].get("project"), "library_built": bool(tc and tc["built"])})
    elif a.cmd == "ingest":
        from .lake import connectors
        con = neo.engine.connection
        if a.kind == "csv":
            print(connectors.ingest_csv(neo.catalog, con, a.args[0], a.args[1]))
        else:
            from .bootstrap import FDIC_SOURCES
            print(connectors.ingest_fdic(neo.catalog, con, a.args or FDIC_SOURCES))
    elif a.cmd == "eval":
        from .evaluation import run_golden
        rep = run_golden(neo, a.path, live=a.live)
        print(f"{rep['passed']}/{rep['cases']} passed ({rep['mode']})")
        for f in rep["failed"]:
            print(f"  FAIL {f['id']}: {f.get('reason')}")
        sys.exit(0 if rep["passed"] == rep["cases"] else 1)
    elif a.cmd == "curation":
        for r in neo.runs.curation_summary():
            print(f"{r['n']:>4}  {r['kind']:<18} {r['item'][:80]}")
    elif a.cmd == "audit":
        ok, n, msg = neo.audit.verify()
        print(msg)
        sys.exit(0 if ok else 1)
    elif a.cmd == "stats":
        out(neo.runs.stats())
    elif a.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(neo, workers=a.workers), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
