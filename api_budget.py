"""Persisted daily Claude spending guard; all production calls pass through it."""

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from job_quality import atomic_json


class BudgetExceeded(RuntimeError):
    pass


class DailyBudget:
    # Published Sonnet 4.6 USD/token rates, checked 2026-09-23:
    # https://platform.claude.com/docs/en/models/sonnet-4-6/overview
    INPUT = 3 / 1_000_000
    OUTPUT = 15 / 1_000_000
    CACHE_WRITE = 3.75 / 1_000_000
    CACHE_READ = .30 / 1_000_000

    def __init__(self, path, limit=.75):
        self.path = Path(path)
        self.limit = limit

    def load(self):
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        ledger = json.loads(self.path.read_text()) if self.path.exists() else {}
        if ledger.get("day") != today:
            ledger = {"day": today, "usd": 0, "calls": 0, "input_tokens": 0,
                      "output_tokens": 0, "cache_read_tokens": 0, "uncertain_calls": 0}
        return ledger

    def create(self, client, **kwargs):
        if kwargs.get("model") != "claude-sonnet-4-6":
            raise ValueError("Configure verified pricing before changing models")
        ledger = self.load()
        if ledger["usd"] >= self.limit:
            raise BudgetExceeded("Daily API budget reached")
        # Token counting is unbilled. Reserve a conservative full-output charge
        # BEFORE making the billable request, including cache-write overhead.
        count_args = {k: kwargs[k] for k in ("model", "messages", "system") if k in kwargs}
        count = client.messages.count_tokens(**count_args).input_tokens
        reserve = (count * 1.05 + 256) * self.CACHE_WRITE + kwargs["max_tokens"] * self.OUTPUT
        if ledger["usd"] + reserve > self.limit:
            raise BudgetExceeded("Next request could exceed the daily API budget")
        ledger["usd"] += reserve
        ledger["calls"] += 1
        ledger["uncertain_calls"] += 1
        atomic_json(self.path, ledger)
        # On timeout/process death, leave the reservation charged: the server may
        # have completed the request. SDK automatic retries are disabled upstream.
        message = client.messages.create(**kwargs)
        usage = message.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        actual = input_tokens * self.INPUT + output_tokens * self.OUTPUT + cache_write * self.CACHE_WRITE + cache_read * self.CACHE_READ
        ledger["usd"] += actual - reserve
        ledger["uncertain_calls"] -= 1
        ledger["input_tokens"] += input_tokens + cache_write
        ledger["output_tokens"] += output_tokens
        ledger["cache_read_tokens"] += cache_read
        atomic_json(self.path, ledger)
        return message
