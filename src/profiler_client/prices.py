"""Token prices for estimating cost when a trace records tokens but no cost.

Prices are $ per 1M input and output tokens, given per model, with ``*``
for every model not listed:

    --prices 1.25,10                        every model
    --prices gpt-5=1.25,10 --prices gpt-5-mini=0.25,2 --prices 3,15
    --prices-file prices.json               {"gpt-5": [1.25, 10], "*": [3, 15]}

A model matches only its exact id. The server estimates with
``price_for``, so the client and the server match models the same way.
"""
from __future__ import annotations

import json
from pathlib import Path

ALL = "*"
MAX_PRICE = 1e6
FORMAT = "IN,OUT or MODEL=IN,OUT in $ per 1M input and output tokens"

Prices = dict[str, tuple[float, float]]


def _pair(value, where: str) -> tuple[float, float]:
    if isinstance(value, str):
        value = value.split(",")
    try:
        given, out = (float(x) for x in value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: give two prices, {FORMAT}") from None
    if not (0 <= given <= MAX_PRICE and 0 <= out <= MAX_PRICE):
        raise ValueError(f"{where}: token prices can't be negative or over {MAX_PRICE:g}")
    return given, out


def parse(values: list[str] | None = None, file: str | Path | None = None) -> Prices | None:
    """The prices from ``--prices`` values and a ``--prices-file``, or None
    when neither is given. A ``--prices`` value overrides the file's price
    for the same model."""
    prices: Prices = {}
    if file is not None:
        try:
            data = json.loads(Path(file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"--prices-file {file}: {exc}") from None
        if not isinstance(data, dict) or not data:
            raise ValueError(f'--prices-file {file}: give an object of model: [IN, OUT], e.g. {{"gpt-5": [1.25, 10]}}')
        prices.update({str(model): _pair(pair, f"--prices-file {model}") for model, pair in data.items()})
    for value in values or []:
        model, _, pair = value.rpartition("=")
        prices[model.strip() or ALL] = _pair(pair, f"--prices {value}")
    return prices or None


def price_for(prices: Prices, model: str | None) -> tuple[float, float] | None:
    """The price of ``model``'s tokens: its own, else ``*``, else None."""
    return prices.get(model) if model in prices else prices.get(ALL)


def keyed(prices: Prices, name) -> Prices:
    """The prices under the model values the events carry: ``name`` is how
    ``prepare`` names a model (``Names.model``), clear text for a public
    model id and a pseudonym for any other."""
    return {model if model == ALL else name(model): pair for model, pair in prices.items()}


def as_param(prices: Prices) -> list[float] | dict[str, list[float]]:
    """The job parameter: the old ``[IN, OUT]`` when one price covers every
    model, else ``{model: [IN, OUT]}``."""
    if list(prices) == [ALL]:
        return list(prices[ALL])
    return {model: list(pair) for model, pair in prices.items()}
