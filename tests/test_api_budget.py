import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from api_budget import DailyBudget, BudgetExceeded

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.budget=DailyBudget(Path(self.tmp.name)/'usage.json',.50)
        self.client=Mock()
        self.client.messages.count_tokens.return_value=SimpleNamespace(input_tokens=1000)
        self.client.messages.create.return_value=SimpleNamespace(usage=SimpleNamespace(input_tokens=1000,output_tokens=100,cache_creation_input_tokens=0,cache_read_input_tokens=0))
        self.kwargs=dict(model='claude-sonnet-4-6',messages=[{'role':'user','content':'fixture'}],max_tokens=500)

    def test_success_replaces_reservation_with_actual_cost(self):
        self.budget.create(self.client,**self.kwargs)
        self.assertAlmostEqual(self.budget.load()['usd'],.0045)
        self.assertEqual(self.budget.load()['uncertain_calls'],0)

    def test_budget_defers_before_billable_request(self):
        self.budget.limit=.001
        with self.assertRaises(BudgetExceeded):self.budget.create(self.client,**self.kwargs)
        self.client.messages.create.assert_not_called()

    def test_timeout_keeps_full_reservation(self):
        self.client.messages.create.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.budget.create(self.client,**self.kwargs)
        self.assertGreater(self.budget.load()['usd'],.0045)
        self.assertEqual(self.budget.load()['uncertain_calls'],1)

    def test_second_run_uses_persisted_daily_total(self):
        self.budget.create(self.client,**self.kwargs)
        next_run=DailyBudget(self.budget.path,.01)
        with self.assertRaises(BudgetExceeded):next_run.create(self.client,**self.kwargs)
        self.assertEqual(self.client.messages.create.call_count,1)

    def test_cache_pricing(self):
        self.client.messages.create.return_value.usage=SimpleNamespace(input_tokens=100,output_tokens=100,cache_creation_input_tokens=0,cache_read_input_tokens=900)
        self.budget.create(self.client,**self.kwargs)
        self.assertAlmostEqual(self.budget.load()['usd'],.00207)

    def test_unpriced_model_never_called(self):
        self.kwargs['model']='other-model'
        with self.assertRaises(ValueError):self.budget.create(self.client,**self.kwargs)
        self.client.messages.create.assert_not_called()

    def test_previous_day_resets(self):
        self.budget.path.write_text(json.dumps({'day':'2020-01-01','usd':10}))
        self.budget.create(self.client,**self.kwargs)
        self.assertLess(self.budget.load()['usd'],.50)
