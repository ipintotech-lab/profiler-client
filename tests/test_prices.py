import pytest

from profiler_client import prices
from profiler_client.readiness import check_data_quality


def test_one_pair_is_every_model_and_stays_the_old_parameter():
    p = prices.parse(["1.25,10"])
    assert p == {"*": (1.25, 10.0)}
    assert prices.as_param(p) == [1.25, 10.0]


def test_a_flag_overrides_the_file_for_the_same_model(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"gpt-5": [1.25, 10], "*": [3, 15]}', encoding="utf-8")
    p = prices.parse(["gpt-5=2,20"], f)
    assert p == {"gpt-5": (2.0, 20.0), "*": (3.0, 15.0)}
    assert prices.as_param(p) == {"gpt-5": [2.0, 20.0], "*": [3.0, 15.0]}


def test_a_model_matches_its_exact_id_else_the_default():
    p = {"gpt-5": (1.25, 10.0), "*": (3.0, 15.0)}
    assert prices.price_for(p, "gpt-5") == (1.25, 10.0)
    assert prices.price_for(p, "gpt-5-mini") == (3.0, 15.0)
    assert prices.price_for({"gpt-5": (1.25, 10.0)}, "gpt-5-mini") is None


def test_prices_are_keyed_by_the_value_the_events_carry():
    keyed = prices.keyed({"acme-v3": (1.0, 2.0), "*": (3.0, 15.0)}, lambda m: f"h:{m}")
    assert keyed == {"h:acme-v3": (1.0, 2.0), "*": (3.0, 15.0)}


def test_no_prices_given_is_none():
    assert prices.parse() is None and prices.parse([]) is None


@pytest.mark.parametrize("value", ["1", "a,b", "-1,2", "1,2,3", "m=1"])
def test_bad_prices_are_refused(value):
    with pytest.raises(ValueError):
        prices.parse([value])


class _E:
    def __init__(self, cost):
        self.cost, self.tool, self.event_type, self.model = cost, None, "decision", "gpt-5"


def test_an_estimate_per_model_names_each_price_and_the_unpriced_calls(monkeypatch):
    from profiler_client import readiness
    monkeypatch.setattr(readiness, "_ATTRIBUTES", [("cost", "Cost", "llm", lambda e: e.cost > 0, "no cost")])
    monkeypatch.setattr(readiness, "is_llm", lambda e: True)
    estimated = {"events": 3, "unpriced": 1, "by_model": [
        {"model": "gpt-5", "input_per_mtok": 1.25, "output_per_mtok": 10, "events": 2},
        {"model": "gpt-5-mini", "input_per_mtok": 0.25, "output_per_mtok": 2, "events": 1}]}
    (gap,) = check_data_quality([_E(1.0)], estimated)["gaps"]
    assert gap["status"] == "estimated"
    assert "gpt-5 $1.25 / $10 on 2 calls; gpt-5-mini $0.25 / $2 on 1 call" in gap["effect"]
    assert "1 LLM call has tokens but no price for its model" in gap["effect"]


def test_the_old_single_price_estimate_reads_as_before(monkeypatch):
    from profiler_client import readiness
    monkeypatch.setattr(readiness, "_ATTRIBUTES", [("cost", "Cost", "llm", lambda e: e.cost > 0, "no cost")])
    monkeypatch.setattr(readiness, "is_llm", lambda e: True)
    (gap,) = check_data_quality([_E(1.0)], {"events": 3, "input_per_mtok": 1.25, "output_per_mtok": 10})["gaps"]
    assert gap["effect"].startswith("Cost is estimated for 3 LLM calls from their tokens ($1.25 / $10 per 1M input / output tokens).")
