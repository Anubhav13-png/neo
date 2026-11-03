"""Ontology storage: the source of truth, with immutable published versions.

`OntologyStore` is the adapter to implement for your own ontology storage (graph database, RDF store,
catalog, internal service). It only has to return a document in the format of `model.py` for a version,
list versions, and accept a published document. `JsonDirStore` is the local implementation:

  <root>/source/*.json     authoring files, merged on publish (edit these)
  <root>/versions/*.json   published versions, written once
  <root>/CURRENT           the version questions use by default
"""
from __future__ import annotations

import glob
import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional

from ..util import now_iso, sha, stable_json
from .model import KINDS, Ontology


class OntologyStore(ABC):
    @abstractmethod
    def current_version(self) -> Optional[str]: ...

    @abstractmethod
    def load_document(self, version: str) -> dict: ...

    @abstractmethod
    def publish(self, document: dict, note: str = "", make_current: bool = True) -> str: ...

    @abstractmethod
    def list_versions(self) -> List[dict]: ...

    def load(self, version: Optional[str] = None) -> Ontology:
        version = version or self.current_version()
        if version is None:
            raise FileNotFoundError("no ontology version is published yet (run `neo ontology publish`)")
        return Ontology(self.load_document(version), version)


class JsonDirStore(OntologyStore):
    def __init__(self, root: str):
        self.root = root
        os.makedirs(os.path.join(root, "versions"), exist_ok=True)
        os.makedirs(os.path.join(root, "source"), exist_ok=True)

    def current_version(self) -> Optional[str]:
        path = os.path.join(self.root, "CURRENT")
        return open(path).read().strip() if os.path.exists(path) else None

    def load_document(self, version: str) -> dict:
        path = os.path.join(self.root, "versions", f"{version}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"ontology version `{version}` not found")
        return json.load(open(path))["document"]

    def source_document(self) -> dict:
        """Merge every authoring file under source/ into one document."""
        doc = {k: [] for k in KINDS}
        for path in sorted(glob.glob(os.path.join(self.root, "source", "*.json"))):
            part = json.load(open(path))
            for k in KINDS:
                doc[k] += part.get(k, [])
        return doc

    def list_versions(self) -> List[dict]:
        out = []
        for path in sorted(glob.glob(os.path.join(self.root, "versions", "*.json"))):
            meta = json.load(open(path))
            out.append({"version": meta["version"], "published": meta["published"], "note": meta.get("note", ""),
                        "objects": {k: len(v) for k, v in meta["document"].items()}})
        return out

    def publish(self, document: dict, note: str = "", make_current: bool = True) -> str:
        n = len(glob.glob(os.path.join(self.root, "versions", "*.json"))) + 1
        version = f"v{n:04d}-{sha(stable_json(document), 8)}"
        path = os.path.join(self.root, "versions", f"{version}.json")
        if os.path.exists(path):
            raise FileExistsError(path)
        json.dump({"version": version, "published": now_iso(), "note": note, "document": document},
                  open(path, "w"), indent=1)
        if make_current:
            self.set_current(version)
        return version

    def set_current(self, version: str):
        tmp = os.path.join(self.root, "CURRENT.tmp")
        open(tmp, "w").write(version)
        os.replace(tmp, os.path.join(self.root, "CURRENT"))


def make_store(cfg: dict) -> OntologyStore:
    kind = cfg["ontology"].get("store", "json_dir")
    if kind == "json_dir":
        return JsonDirStore(cfg["ontology"]["root"])
    raise ValueError(f"unknown ontology.store `{kind}`: implement OntologyStore for your storage")
