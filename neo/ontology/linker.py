"""Entity linking: which ontology objects does a question talk about?

Two signals, combined: exact phrase matches against names and aliases (precise), and TF-IDF cosine
similarity between the question and each object's text (recall, for wording that no alias covers).
Pure Python, no external index; swap in a vector store behind the same `link()` at larger scale.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .model import Entity, Field, Metric, Ontology, ScenarioSet, Term

_WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an the of in on at to for by with and or is are was were be been what which who how many much "
           "does do did has have had it its this that these those from as than then there their our we i you "
           "can could would should will per each every all any some into over under between".split())


def stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str) -> List[str]:
    return [stem(w) for w in _WORD.findall(text.lower().replace("_", " "))]


@dataclass
class Link:
    id: str
    kind: str
    score: float
    matched: str


class Linker:
    def __init__(self, onto: Ontology):
        self.onto = onto
        self.phrases: Dict[Tuple[str, ...], List[Tuple[str, str]]] = defaultdict(list)
        self.docs: Dict[str, Counter] = {}
        self.kinds: Dict[str, str] = {}
        for t in onto.terms.values():
            self._add(t.id, "term", [t.name] + t.aliases, f"{t.name} {' '.join(t.aliases)} {t.definition}")
        for e in onto.entities.values():
            self._add(e.id, "entity", [e.name, e.table] + e.aliases, f"{e.name} {' '.join(e.aliases)} {e.definition}")
        for ent, fs in onto.fields.items():
            for f in fs.values():
                term = onto.terms.get(f.term) if f.term else None
                names = [f.name] + f.aliases + ([term.name] + term.aliases if term else [])
                self._add(f.id, "field", names, f"{' '.join(names)} {f.description}")
        for m in onto.metrics.values():
            self._add(m.id, "metric", [m.name] + m.aliases, f"{m.name} {' '.join(m.aliases)} {m.description}")
        for s in onto.scenario_sets.values():
            self._add(s.id, "scenario_set", [s.name] + s.aliases, f"{s.name} {' '.join(s.aliases)} {s.description}")
        n = max(1, len(self.docs))
        df = Counter(w for d in self.docs.values() for w in d)
        self.idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}

    def _add(self, ident: str, kind: str, names: List[str], text: str):
        self.kinds[ident] = kind
        for nm in names:
            toks = tuple(w for w in tokens(nm or "") if w)
            if toks:
                self.phrases[toks].append((ident, nm))
        self.docs[ident] = Counter(w for w in tokens(text) if w not in STOP)

    def _tfidf(self, counter: Counter) -> Dict[str, float]:
        vec = {w: c * self.idf.get(w, 0.0) for w, c in counter.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {w: v / norm for w, v in vec.items()}

    def link(self, question: str, limit: int = 25, threshold: float = 0.18) -> List[Link]:
        toks = tokens(question)
        best: Dict[str, Link] = {}
        for n in range(min(6, len(toks)), 0, -1):
            for i in range(len(toks) - n + 1):
                gram = tuple(toks[i:i + n])
                if n == 1 and gram[0] in STOP:
                    continue
                for ident, name in self.phrases.get(gram, []):
                    score = min(1.0, 0.6 + 0.15 * n)
                    if ident not in best or best[ident].score < score:
                        best[ident] = Link(ident, self.kinds[ident], score, name)
        q = self._tfidf(Counter(w for w in toks if w not in STOP))
        for ident, doc in self.docs.items():
            d = self._tfidf(doc)
            cos = sum(v * d.get(w, 0.0) for w, v in q.items())
            score = 0.8 * cos
            if score >= threshold and (ident not in best or best[ident].score < score):
                best[ident] = Link(ident, self.kinds[ident], round(score, 3), "similar wording")
        return sorted(best.values(), key=lambda l: -l.score)[:limit]

    def unlinked_words(self, question: str) -> List[str]:
        """Content words that matched nothing: candidates for new aliases (fed to curation)."""
        known = {w for gram in self.phrases for w in gram}
        return [w for w in tokens(question) if w not in STOP and w not in known and not w.isdigit() and len(w) > 2]
