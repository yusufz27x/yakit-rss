"""Tests for the yakit-rss feed generator (standard library only)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yakit_rss as app  # noqa: E402


PRICE_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>City</th>
      <th>V/Max Kurşunsuz 95</th>
      <th>V/Max Diesel</th>
      <th>PO/gaz Otogaz</th>
    </tr>
  </thead>
  <tbody>
    <tr class="price-row" data-disctrict-name="ISTANBUL (AVRUPA)">
      <td>ISTANBUL (AVRUPA)</td>
      <td><span class="with-tax">76.30</span><span class="without-tax">63.59</span> TL/LT</td>
      <td><span class="with-tax">96.80</span><span class="without-tax">80.67</span> TL/LT</td>
      <td><span class="with-tax">41.20</span><span class="without-tax">34.33</span> TL/LT</td>
    </tr>
    <tr class="price-row" data-disctrict-name="ANKARA">
      <td>ANKARA</td>
      <td><span class="with-tax">77.30</span><span class="without-tax">64.42</span> TL/LT</td>
      <td><span class="with-tax">97.90</span><span class="without-tax">81.59</span> TL/LT</td>
      <td><span class="with-tax">41.29</span><span class="without-tax">34.41</span> TL/LT</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


YAHOO_JSON = json.dumps(
    {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": "BZ=F",
                        "regularMarketPrice": 110.20,
                        "regularMarketTime": 1793000000,
                    },
                    "timestamp": [1792800000, 1792900000, 1793000000],
                    "indicators": {"quote": [{"close": [108.00, 109.00, 110.20]}]},
                }
            ],
            "error": None,
        }
    }
)

RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Enerji Test</title>
    <item>
      <title>Benzine indirim geldi</title>
      <link>https://example.com/benzin</link>
      <pubDate>Mon, 26 Oct 2026 10:00:00 +0300</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Enerji Atom</title>
  <entry>
    <title>Motorine zam geliyor</title>
    <link rel="alternate" href="https://example.com/motorin"/>
    <updated>2026-10-26T10:00:00+03:00</updated>
  </entry>
</feed>
"""

TCMB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Tarih_Date Tarih="23.10.2026" Date="10/23/2026">
  <Currency CrossOrder="0" Kod="USD">
    <Unit>1</Unit>
    <ForexBuying>49.5647</ForexBuying>
    <ForexSelling>49.6540</ForexSelling>
  </Currency>
</Tarih_Date>
"""


def market_series(
    symbol: str,
    price: float,
    *,
    label: str = "",
    previous: float | None = None,
    history: list[float] | None = None,
) -> app.MarketSeries:
    return app.MarketSeries(
        symbol=symbol,
        label=label or symbol,
        price=price,
        previous=previous,
        history=history if history is not None else [price],
        currency="USD",
        market_time=None,
        source="test",
    )


def news(title: str, published: datetime, publisher: str = "Test Haber") -> app.NewsItem:
    return app.NewsItem(
        title=title,
        link="https://example.com/news",
        published=published,
        publisher=publisher,
        publisher_url="https://example.com",
    )


class ParsingTests(unittest.TestCase):
    def test_fold_text_handles_turkish_letters(self) -> None:
        self.assertEqual(app.fold_text("İSTANBUL (AVRUPA)"), "istanbul (avrupa)")
        self.assertEqual(app.fold_text("IĞDIR"), "igdir")
        self.assertEqual(app.fold_text("Şanlıurfa"), "sanliurfa")

    def test_parse_price_numbers(self) -> None:
        self.assertEqual(app.parse_price_number("76.30"), 76.30)
        self.assertEqual(app.parse_price_number("1.234,56"), 1234.56)
        self.assertEqual(app.parse_price_number("76,30 TL"), 76.30)
        with self.assertRaises(ValueError):
            app.parse_price_number("TL/LT")

    def test_parse_price_table(self) -> None:
        rows = app.parse_prices(PRICE_HTML, ["benzin", "motorin", "lpg"])
        self.assertEqual([row.name for row in rows], ["ISTANBUL (AVRUPA)", "ANKARA"])
        self.assertEqual(rows[0].prices, {"benzin": 76.30, "motorin": 96.80, "lpg": 41.20})
        self.assertEqual(rows[1].prices["motorin"], 97.90)

    def test_find_city(self) -> None:
        rows = app.parse_prices(PRICE_HTML, ["benzin", "motorin", "lpg"])
        self.assertEqual(app.find_city(rows, "istanbul (avrupa)").name, "ISTANBUL (AVRUPA)")
        self.assertEqual(app.find_city(rows, "ankara").name, "ANKARA")
        ambiguous = [
            app.PriceRow("ISTANBUL (AVRUPA)", {}),
            app.PriceRow("ISTANBUL (ANADOLU)", {}),
        ]
        with self.assertRaises(app.ConfigError):
            app.find_city(ambiguous, "ISTANBUL")
        with self.assertRaises(app.ConfigError):
            app.find_city(rows, "Atlantis")
        self.assertEqual(
            app.find_city([app.PriceRow("AFYON", {})], "Afyonkarahisar").name,
            "AFYON",
        )

    def test_clean_news_title(self) -> None:
        self.assertEqual(
            app.clean_news_title("Benzine indirim - NTV Haber", "NTV Haber"),
            "Benzine indirim",
        )
        self.assertEqual(
            app.clean_news_title("Benzine indirim", "NTV Haber"),
            "Benzine indirim",
        )

    def test_tr_number(self) -> None:
        self.assertEqual(app.tr_number(76.3), "76,30")
        self.assertEqual(app.tr_number(1234.5), "1.234,50")


class NewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=self.tz)
        self.cfg = dict(app.DEFAULT_CONFIG)
        self.cfg["news_filter"] = "changes"
        self.cfg["news_max_age_days"] = 3

    def test_relevance_filter(self) -> None:
        self.assertTrue(
            app.news_is_relevant(news("Benzine indirim geldi", self.now), "changes")
        )
        self.assertTrue(
            app.news_is_relevant(news("Motorine zam yolda", self.now), "changes")
        )
        self.assertFalse(
            app.news_is_relevant(news("Benzin fiyatları ne kadar?", self.now), "changes")
        )
        self.assertFalse(
            app.news_is_relevant(news("Motorine zam var mı?", self.now), "changes")
        )
        self.assertFalse(
            app.news_is_relevant(news("Benzine indirim mi geliyor?", self.now), "changes")
        )
        self.assertFalse(
            app.news_is_relevant(news("Borsa rekor kırdı", self.now), "changes")
        )
        self.assertFalse(
            app.news_is_relevant(
                news("Benzin fiyatları zamanla değişiyor", self.now), "changes"
            )
        )
        self.assertTrue(
            app.news_is_relevant(news("Benzin fiyatları ne kadar?", self.now), "all")
        )

    def test_status_detection(self) -> None:
        self.assertEqual(app.news_status(app.fold_text("Benzine indirim geldi")), "confirmed")
        self.assertEqual(app.news_status(app.fold_text("Benzine indirim geliyor")), "expected")
        self.assertEqual(app.news_status(app.fold_text("Benzine indirim iptal edildi")), "iptal")
        self.assertEqual(app.news_status(app.fold_text("Benzine indirim")), "other")
        self.assertEqual(
            app.news_status(app.fold_text("Benzine indirim yarından itibaren geçerli olacak")),
            "expected",
        )
        self.assertEqual(
            app.news_status(app.fold_text("Benzine indirim yarından itibaren geçerli")),
            "confirmed",
        )

    def test_select_news_clusters_duplicates(self) -> None:
        items = [
            news("Benzine indirim geliyor", self.now - timedelta(hours=2)),
            news("Benzine indirim geliyor! Tabelalar değişecek", self.now - timedelta(hours=1)),
            news("Benzine indirim", self.now - timedelta(hours=3)),
            news("Motorine zam kapıda", self.now - timedelta(hours=1)),
        ]
        selected = app.select_news(items, self.cfg, app.empty_state(), self.now, self.tz)
        self.assertEqual(len(selected), 2)
        titles = {item.title for _, item in selected}
        self.assertIn("Motorine zam kapıda", titles)
        # The longer title with an amount/context wins, or at least only one
        # benzine item survives.
        self.assertEqual(sum("Benzine" in title for title in titles), 1)

    def test_select_news_respects_seen_events(self) -> None:
        item = news("Benzine indirim geliyor", self.now - timedelta(hours=1))
        state = app.empty_state()
        first = app.select_news([item], self.cfg, state, self.now, self.tz)
        self.assertEqual(len(first), 1)
        key = first[0][0]
        state["news_events"][key] = item.published.isoformat()
        second = app.select_news([item], self.cfg, state, self.now, self.tz)
        self.assertEqual(second, [])

    def test_make_news_item_uses_local_time(self) -> None:
        item = news("Benzine indirim geldi", self.now)
        result = app.make_news_item(item, "benzin|indirim|2026-10-23|confirmed", self.tz)
        self.assertTrue(result["title"].startswith("[Haber] "))
        self.assertTrue(result["published"].endswith("+03:00"))
        self.assertIn("Kaynak:", result["description"])

    def test_parse_rss_feed(self) -> None:
        source = {"name": "Enerji Test", "url": "https://example.com/rss"}
        items = app.parse_feed(RSS_FEED, source, self.tz)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Benzine indirim geldi")
        self.assertEqual(items[0].link, "https://example.com/benzin")
        self.assertEqual(items[0].publisher, "Enerji Test")
        self.assertEqual(items[0].published.utcoffset().total_seconds(), 10800)

    def test_parse_atom_feed(self) -> None:
        source = {"name": "Enerji Atom", "url": "https://example.com/atom"}
        items = app.parse_feed(ATOM_FEED, source, self.tz)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Motorine zam geliyor")
        self.assertEqual(items[0].link, "https://example.com/motorin")
        self.assertEqual(items[0].published.year, 2026)


class MarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=self.tz)

    def test_parse_yahoo_chart(self) -> None:
        series = app.parse_yahoo_chart(
            YAHOO_JSON, symbol="BZ=F", label="Brent", tz=self.tz
        )
        self.assertAlmostEqual(series.price, 110.20)
        self.assertAlmostEqual(series.previous or 0, 109.00)
        self.assertAlmostEqual(series.change_pct or 0, 1.1009, places=3)
        self.assertEqual(series.history[-1], 110.20)
        self.assertEqual(series.source, "Yahoo Finance")

    def test_yahoo_live_price_is_added_to_history(self) -> None:
        payload = json.loads(YAHOO_JSON)
        payload["chart"]["result"][0]["meta"]["regularMarketPrice"] = 110.40
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [
            108.00,
            109.00,
            110.00,
        ]
        series = app.parse_yahoo_chart(
            json.dumps(payload), symbol="BZ=F", label="Brent", tz=self.tz
        )
        self.assertEqual(series.history, [108.00, 109.00, 110.00, 110.40])
        self.assertEqual(series.previous, 110.00)

    def test_parse_tcmb_usd_try(self) -> None:
        series = app.parse_tcmb_usd_try(TCMB_XML, self.tz)
        self.assertAlmostEqual(series.price, 49.6540)
        self.assertEqual(series.source, "TCMB")
        self.assertEqual(series.market_time.day, 23)

    def test_market_cost_for_products(self) -> None:
        market = {
            "brent": market_series("BZ=F", 110.20),
            "gasoline": market_series("RB=F", 3.1084),
            "diesel": market_series("HO=F", 4.7340),
            "usdtry": market_series("TRY=X", 49.7575),
        }
        gasoline = app.market_cost_for_product("benzin", market)
        diesel = app.market_cost_for_product("motorin", market)
        self.assertIsNotNone(gasoline)
        self.assertIsNotNone(diesel)
        assert gasoline is not None and diesel is not None
        self.assertAlmostEqual(gasoline[0], 3.1084 / 3.785411784 * 49.7575, places=4)
        self.assertAlmostEqual(diesel[0], 4.7340 / 3.785411784 * 49.7575, places=4)
        self.assertEqual(gasoline[1], "RBOB vadeli")
        self.assertEqual(diesel[1], "ULSD vadeli")

    def test_market_cost_falls_back_to_brent(self) -> None:
        market = {
            "brent": market_series("BZ=F", 110.20),
            "usdtry": market_series("TRY=X", 49.7575),
        }
        result = app.market_cost_for_product("benzin", market)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[1], "Brent proxy")
        self.assertEqual(result[2], "düşük")

    def test_build_sparkline(self) -> None:
        self.assertEqual(app.build_sparkline([]), "")
        svg = app.build_sparkline([100.0, 102.5, 101.0])
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("polyline", svg)


class PredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=self.tz)
        self.cfg = dict(app.DEFAULT_CONFIG)
        self.cfg["prediction_vat"] = 0.20
        self.cfg["prediction_pass_through"] = 1.0
        self.cfg["prediction_min_delta_tl"] = 0.25

    def test_baseline_creation_and_price_reset(self) -> None:
        state = app.empty_state()
        baseline, reset = app.update_prediction_baseline(
            state, "ANKARA", "benzin", 76.30, 40.00, "RBOB vadeli", self.now
        )
        self.assertTrue(reset)
        self.assertEqual(baseline["pump_price"], 76.30)

        baseline, reset = app.update_prediction_baseline(
            state, "ANKARA", "benzin", 76.30, 40.50, "RBOB vadeli", self.now
        )
        self.assertFalse(reset)
        self.assertEqual(baseline["cost_tl_l"], 40.00)

        baseline, reset = app.update_prediction_baseline(
            state, "ANKARA", "benzin", 77.00, 40.50, "RBOB vadeli", self.now
        )
        self.assertTrue(reset)
        self.assertEqual(baseline["pump_price"], 77.00)
        self.assertEqual(baseline["cost_tl_l"], 40.50)

    def test_compute_prediction(self) -> None:
        baseline = {
            "pump_price": 76.30,
            "cost_tl_l": 40.00,
            "source": "RBOB vadeli",
            "basis_at": self.now.isoformat(),
        }
        prediction = app.compute_prediction(
            self.cfg,
            "benzin",
            76.30,
            41.00,
            "RBOB vadeli",
            "orta",
            baseline,
            False,
            self.now,
            True,
        )
        self.assertAlmostEqual(prediction.delta, 1.20)
        self.assertAlmostEqual(prediction.predicted_price, 77.50)
        self.assertEqual(prediction.direction, "zam")
        self.assertEqual(prediction.confidence, "orta")

    def test_compute_prediction_for_discount(self) -> None:
        baseline = {
            "pump_price": 96.80,
            "cost_tl_l": 40.00,
            "source": "ULSD vadeli",
            "basis_at": self.now.isoformat(),
        }
        prediction = app.compute_prediction(
            self.cfg,
            "motorin",
            96.80,
            39.00,
            "ULSD vadeli",
            "orta",
            baseline,
            False,
            self.now,
            True,
        )
        self.assertAlmostEqual(prediction.delta, -1.20)
        self.assertAlmostEqual(prediction.predicted_price, 95.60)
        self.assertEqual(prediction.direction, "indirim")

    def test_compute_prediction_with_esel_pass_through(self) -> None:
        self.cfg["prediction_pass_through"] = 0.25
        baseline = {
            "pump_price": 76.30,
            "cost_tl_l": 40.00,
            "source": "RBOB vadeli",
            "basis_at": self.now.isoformat(),
        }
        prediction = app.compute_prediction(
            self.cfg,
            "benzin",
            76.30,
            41.00,
            "RBOB vadeli",
            "orta",
            baseline,
            False,
            self.now,
            True,
        )
        self.assertAlmostEqual(prediction.delta, 0.30)

    def test_prediction_feed_item_deduplicates(self) -> None:
        state = app.empty_state()
        prediction = app.Prediction(
            product="benzin",
            label="Benzin",
            current_price=76.30,
            predicted_price=77.50,
            delta=1.20,
            direction="zam",
            confidence="orta",
            source="RBOB vadeli",
            basis_cost=40.00,
            current_cost=41.00,
            basis_at=self.now.isoformat(),
            note="Test",
        )
        market = {
            "brent": market_series("BZ=F", 110.20),
            "usdtry": market_series("TRY=X", 49.7575),
        }
        item = app.make_prediction_item(
            self.cfg,
            state,
            "ANKARA",
            prediction,
            market,
            self.now,
            self.tz,
            "https://example.com/",
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertTrue(item["title"].startswith("[Tahmin] Benzin"))
        self.assertEqual(item["category"], "Tahmin")
        self.assertEqual(len(state["prediction_items"]), 1)

        duplicate = app.make_prediction_item(
            self.cfg,
            state,
            "ANKARA",
            prediction,
            market,
            self.now,
            self.tz,
            "https://example.com/",
        )
        self.assertIsNone(duplicate)

    def test_prediction_item_ignores_small_delta(self) -> None:
        state = app.empty_state()
        prediction = app.Prediction(
            product="benzin",
            label="Benzin",
            current_price=76.30,
            predicted_price=76.50,
            delta=0.20,
            direction="zam",
            confidence="orta",
            source="RBOB vadeli",
            basis_cost=40.00,
            current_cost=40.16,
            basis_at=self.now.isoformat(),
            note="Test",
        )
        item = app.make_prediction_item(
            self.cfg, state, "ANKARA", prediction, {}, self.now, self.tz, ""
        )
        self.assertIsNone(item)


class CityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=self.tz)
        self.rows = app.parse_prices(PRICE_HTML, ["benzin", "motorin", "lpg"])
        self.cfg = dict(app.DEFAULT_CONFIG)
        self.cfg["city"] = "ISTANBUL (AVRUPA)"
        self.cfg["city_label"] = "İstanbul (Avrupa)"

    def test_city_slug(self) -> None:
        self.assertEqual(app.city_slug("ISTANBUL (AVRUPA)"), "istanbul-avrupa")
        self.assertEqual(app.city_slug("ANKARA"), "ankara")
        self.assertEqual(app.city_slug("IĞDIR"), "igdir")
        self.assertEqual(app.city_slug("AFYON"), "afyonkarahisar")

    def test_city_display_name(self) -> None:
        self.assertEqual(app.city_display_name("ISTANBUL (AVRUPA)"), "İstanbul (Avrupa)")
        self.assertEqual(app.city_display_name("ISTANBUL (ANADOLU)"), "İstanbul (Anadolu)")
        self.assertEqual(app.city_display_name("ANKARA"), "Ankara")
        self.assertEqual(app.city_display_name("AFYON"), "Afyonkarahisar")
        self.assertEqual(app.city_display_name("IĞDIR"), "Iğdır")
        self.assertEqual(app.city_display_name("ŞANLIURFA"), "Şanlıurfa")

    def test_select_city_infos_all_and_subset(self) -> None:
        infos = app.select_city_infos(self.rows, self.cfg)
        self.assertEqual([info.name for info in infos], ["ANKARA", "ISTANBUL (AVRUPA)"])
        self.assertIn("İstanbul (Avrupa)", [info.label for info in infos])

        self.cfg["cities"] = ["ANKARA"]
        infos = app.select_city_infos(self.rows, self.cfg)
        self.assertEqual({info.name for info in infos}, {"ANKARA", "ISTANBUL (AVRUPA)"})

    def test_city_feed_items_combines_news_and_local_items(self) -> None:
        state = app.empty_state()
        news_item = app.make_news_item(
            news("Benzine indirim geldi", self.now),
            "benzin|indirim|2026-10-26|confirmed",
            self.tz,
        )
        state["news_items"] = [news_item]
        price_item = app.make_price_item(
            city="ANKARA",
            city_label="Ankara",
            product="benzin",
            old=76.30,
            new=77.00,
            now=self.now,
            tz=self.tz,
            source_url="https://example.com",
        )
        state["city_items"] = {"ANKARA": [price_item]}
        items = app.city_feed_items("ANKARA", state, self.cfg)
        self.assertEqual({item["id"] for item in items}, {news_item["id"], price_item["id"]})

    def test_migrate_v1_state(self) -> None:
        old = {
            "version": 1,
            "city": "ANKARA",
            "prices": {"benzin": 77.30},
            "prediction_baselines": {"benzin": {"pump_price": 77.30}},
            "items": [
                {
                    "id": "news-1",
                    "title": "Haber",
                    "link": "https://example.com",
                    "description": "",
                    "published": self.now.isoformat(),
                    "category": "Haber",
                },
                {
                    "id": "price-1",
                    "title": "Fiyat",
                    "link": "https://example.com",
                    "description": "",
                    "published": self.now.isoformat(),
                    "category": "Fiyat",
                },
            ],
            "news_events": {"event": self.now.isoformat()},
            "prediction_items": {"benzin|2026-10-26|zam|0.5": self.now.isoformat()},
        }
        migrated = app.migrate_state(old)
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["prices"]["ANKARA"]["benzin"], 77.30)
        self.assertEqual(len(migrated["news_items"]), 1)
        self.assertEqual(len(migrated["city_items"]["ANKARA"]), 1)
        self.assertIn("ANKARA|benzin|", next(iter(migrated["prediction_items"])))


class PageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=self.tz)
        self.cfg = dict(app.DEFAULT_CONFIG)
        self.cfg["city"] = "ISTANBUL (AVRUPA)"
        self.cfg["city_label"] = "Test <City>"
        self.city_infos = [
            app.CityInfo("ANKARA", "Ankara", "ankara"),
            app.CityInfo("ISTANBUL (AVRUPA)", "Test <City>", "istanbul-avrupa"),
        ]

    def test_overview_page_contains_prices_and_escapes_city(self) -> None:
        prediction = app.Prediction(
            product="benzin",
            label="Benzin",
            current_price=76.30,
            predicted_price=77.50,
            delta=1.20,
            direction="zam",
            confidence="orta",
            source="RBOB vadeli",
            basis_cost=40.00,
            current_cost=41.00,
            basis_at=self.now.isoformat(),
            note="Test notu",
        )
        market = {
            "brent": market_series("BZ=F", 110.20, history=[108.0, 109.0, 110.2]),
            "gasoline": market_series("RB=F", 3.1084),
            "diesel": market_series("HO=F", 4.7340),
            "usdtry": market_series("TRY=X", 49.7575),
        }
        state = app.empty_state()
        page = app.build_overview_page(
            self.cfg,
            self.city_infos,
            state,
            {
                "ANKARA": {"benzin": 77.30, "motorin": 97.90, "lpg": 41.29},
                "ISTANBUL (AVRUPA)": {
                    "benzin": 76.30,
                    "motorin": 96.80,
                    "lpg": 41.20,
                },
            },
            {"ANKARA": [], "ISTANBUL (AVRUPA)": [prediction]},
            market,
            self.now,
            self.tz,
            True,
        )
        self.assertIn("Test &lt;City&gt;", page)
        self.assertIn("76,30", page)
        self.assertIn("Tahmin", page)
        self.assertIn('href="feeds/istanbul-avrupa.xml"', page)
        self.assertIn("+1,20", page)
        self.assertIn('id="city-select"', page)
        self.assertIn("Ankara", page)
        self.assertIn("feeds/ankara.xml", page)

    def test_overview_page_without_market_data(self) -> None:
        state = app.empty_state()
        page = app.build_overview_page(
            self.cfg,
            self.city_infos,
            state,
            {"ISTANBUL (AVRUPA)": {"benzin": 76.30}},
            {},
            {},
            self.now,
            self.tz,
            False,
        )
        self.assertIn("Piyasa verisi alınamadı", page)
        self.assertIn("son bilinen", page.lower())


class FeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Istanbul")
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=self.tz)
        self.cfg = dict(app.DEFAULT_CONFIG)

    def test_make_price_item(self) -> None:
        item = app.make_price_item(
            city="ANKARA",
            city_label="Ankara",
            product="motorin",
            old=96.80,
            new=98.04,
            now=self.now,
            tz=self.tz,
            source_url="https://example.com",
        )
        self.assertIn("96,80 → 98,04", item["title"])
        self.assertIn("+1,24 TL", item["title"])
        self.assertIn("+1,28%", item["description"])
        self.assertEqual(item["category"], "Fiyat")

    def test_build_feed_is_valid_xml(self) -> None:
        self.cfg["city_label"] = "Ankara"
        items = [
            app.make_baseline_item(
                city="ANKARA",
                city_label="Ankara",
                missing={"benzin": 77.30, "motorin": 97.90},
                now=self.now,
                tz=self.tz,
                source_url="https://example.com",
            ),
            app.make_news_item(
                news("Benzine indirim geldi", self.now),
                "benzin|indirim|2026-10-23|confirmed",
                self.tz,
            ),
        ]
        feed = app.build_feed(
            self.cfg,
            "Ankara",
            {"benzin": 77.30, "motorin": 97.90},
            items,
            self.now,
            self.tz,
            price_ok=True,
        )
        root = ET.fromstring(feed)
        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        self.assertIsNotNone(channel)
        self.assertIn("Ankara", channel.findtext("title"))
        self.assertIn("Güncel", channel.findtext("description"))
        self.assertEqual(len(channel.findall("item")), 2)

    def test_merge_items_deduplicates_and_caps(self) -> None:
        items = [
            {
                "id": f"id-{index}",
                "title": f"Item {index}",
                "link": "https://example.com",
                "description": "",
                "published": (self.now - timedelta(hours=index)).isoformat(),
                "category": "Haber",
            }
            for index in range(5)
        ]
        merged = app.merge_items(items[:3], items[3:], max_items=3)
        self.assertEqual(len(merged), 3)
        self.assertEqual(len({item["id"] for item in merged}), 3)

    @unittest.skipIf(os.name != "posix", "POSIX permissions only")
    def test_atomic_write_sets_readable_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            app.atomic_write(path, b"<rss/>\n")
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


class ConfigTests(unittest.TestCase):
    def test_load_config_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"city": "ANKARA", "products": ["benzin"]}),
                encoding="utf-8",
            )
            cfg, base = app.load_config(path)
            self.assertEqual(cfg["city"], "ANKARA")
            self.assertEqual(cfg["products"], ["benzin"])
            self.assertEqual(base, Path(directory).resolve())

    def test_validate_config_rejects_unknown_product(self) -> None:
        cfg = dict(app.DEFAULT_CONFIG)
        cfg["products"] = ["rocket-fuel"]
        with self.assertRaises(app.ConfigError):
            app.validate_config(cfg)

    def test_validate_config_rejects_unsupported_prediction_product(self) -> None:
        cfg = dict(app.DEFAULT_CONFIG)
        cfg["prediction_products"] = ["lpg"]
        with self.assertRaises(app.ConfigError):
            app.validate_config(cfg)

    def test_validate_config_rejects_bad_news_source(self) -> None:
        cfg = dict(app.DEFAULT_CONFIG)
        cfg["news_sources"] = [{"name": "Broken", "url": "not-a-url"}]
        with self.assertRaises(app.ConfigError):
            app.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
