"""Tests for the core service layer and the HTTP API (vertical slice)."""

from django.test import TestCase
from django.urls import reverse

from apps.core.services.price_service import PriceController


class PriceServiceTests(TestCase):
    def test_price_range_matches_original_formula(self):
        service = PriceController()
        data = service.price_range(
            qtyString="10,100", cost=50.0, profit_max=60.0, profit_min=30.0
        )
        self.assertEqual(set(data.keys()), {10, 100})
        # cost * (1 + percent/100) / Exchange.ToUSD['RMB']  (RMB = 6.87)
        self.assertAlmostEqual(data[10], 50 * 1.6 / 6.87, places=6)
        self.assertAlmostEqual(data[100], 50 * 1.3 / 6.87, places=6)


class CommandApiTests(TestCase):
    def test_list_commands_lists_price(self):
        resp = self.client.get(reverse("command-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("price", resp.json()["commands"])

    def test_run_price_command(self):
        resp = self.client.post(
            reverse("command-run", args=["price"]),
            data={"cost": 50, "profit_max": 60, "profit_min": 30, "qtyString": "10,100"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["command"], "price")
        self.assertIn("10", body["result"])
        self.assertAlmostEqual(float(body["result"]["10"]), 50 * 1.6 / 6.87, places=6)

    def test_run_unknown_command_returns_404(self):
        resp = self.client.post(
            reverse("command-run", args=["nope"]),
            data={},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)
