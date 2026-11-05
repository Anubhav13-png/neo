"""The LLM gateway: the only way any part of neo talks to a language model.

  routing    each task (requirements, formalize, review, answer) goes to the model configured for it
  budgets    per-user daily token budget, checked before every call
  egress     refuses a call whose payload carries row-level data when governance forbids it
  logging    every call (task, model, tokens, latency, prompt hash) is recorded in the run store
  retries    transient failures are retried with backoff
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from ..util import sha
from .providers import LLM, LLMError, Reply, make_llm

ROW_DATA_MARKER = "[[ROW-DATA]]"


class BudgetExceeded(LLMError):
    pass


class LLMGateway:
    def __init__(self, cfg: dict, store=None, providers: Optional[Dict[str, LLM]] = None):
        self.cfg = cfg
        self.store = store
        lc = cfg["llm"]
        self.providers: Dict[str, LLM] = dict(providers or {})
        self.specs = {"default": lc.get("default", {})}
        for task, spec in (lc.get("tasks") or {}).items():
            self.specs[task] = self.specs["default"] if spec == "default" else spec
        self.allow_rows = cfg["governance"].get("allow_row_samples_to_llm", False)

    def llm(self, task: str) -> LLM:
        if task in self.providers:
            return self.providers[task]
        key = task if task in self.specs else "default"
        if key not in self.providers:
            if key != "default" and self.specs[key] is self.specs["default"]:
                self.providers[key] = self.llm("default")
            else:
                self.providers[key] = make_llm(self.specs[key])
        return self.providers[key]

    def routes(self, task: str) -> bool:
        """True when the config (llm.tasks) or the caller's providers route this task. Optional tasks, like the
        requirements writer, are skipped when nothing routes them."""
        return task in self.specs or task in self.providers

    def describe(self, task: str = "formalize") -> str:
        try:
            return self.llm(task).describe()
        except LLMError as e:
            return f"unavailable ({e})"

    def chat(self, task: str, system: str, messages: List[Dict[str, str]], principal=None,
             run_id: Optional[str] = None) -> str:
        payload = system + "".join(m["content"] for m in messages)
        if ROW_DATA_MARKER in payload and not self.allow_rows:
            raise LLMError("refused: the request carries row-level data and governance.allow_row_samples_to_llm is off")
        if principal is not None and self.store is not None:
            used = self.store.tokens_today(principal.id)
            if used >= principal.daily_token_budget:
                raise BudgetExceeded(f"daily token budget of {principal.daily_token_budget:,} reached for "
                                     f"{principal.id} ({used:,} used)")
        llm = self.llm(task)
        last = None
        for attempt in range(3):
            t0 = time.time()
            try:
                reply: Reply = llm.chat(system, messages)
                if self.store is not None:
                    self.store.log_llm_call(run_id, principal.id if principal else None, task, llm.describe(),
                                            reply.input_tokens, reply.output_tokens,
                                            round((time.time() - t0) * 1000), sha(payload, 16))
                return reply.text
            except LLMError as e:
                last = e
                if "rate limit" not in str(e).lower() and "could not reach" not in str(e).lower():
                    raise
                time.sleep(2 ** attempt)
        raise last
