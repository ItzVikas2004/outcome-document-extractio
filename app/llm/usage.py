"""Usage is observed, never guessed from response text length."""
from decimal import Decimal, InvalidOperation
import math


def usage_values(payload, config):
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    values = {}
    for source, target in (("prompt_tokens", "tokens_in"), ("completion_tokens", "tokens_out")):
        value = usage.get(source)
        values[target] = value if type(value) is int and value >= 0 else None
    try:
        reported = Decimal(str(usage.get("cost")))
        if not reported.is_finite() or reported < 0 or not math.isfinite(float(reported)):
            raise InvalidOperation
        values.update(cost_usd=float(reported), cost_source="provider_reported")
    except (InvalidOperation, ValueError):
        rates = (config.input_price_per_million, config.output_price_per_million)
        if all(v is not None for v in (*rates, values["tokens_in"], values["tokens_out"])):
            cost = (rates[0] * values["tokens_in"] + rates[1] * values["tokens_out"]) / 1_000_000
            values.update(cost_usd=float(cost), cost_source="configured_rate_estimate")
        else:
            values.update(cost_usd=None, cost_source="unavailable")
    return values


def summarize(calls):
    def totals(items):
        known = [call for call in items if call.get("cost_usd") is not None]
        sources = {call.get("cost_source", "unavailable") for call in items}
        return {"calls": len(items), "tokens_in": sum(c.get("tokens_in") or 0 for c in items),
                "tokens_out": sum(c.get("tokens_out") or 0 for c in items),
                "cost_usd": float(sum((Decimal(str(c["cost_usd"])) for c in known), Decimal(0))),
                "usage_complete": all(c.get("tokens_in") is not None and c.get("tokens_out") is not None for c in items),
                "cost_complete": len(known) == len(items),
                "cost_source": next(iter(sources)) if len(sources) == 1 else "mixed",
                "cost_is_known_subtotal": len(known) != len(items)}
    total = totals(calls)
    total["model_calls"] = total.pop("calls")
    total["unconfirmed_calls"] = sum(call.get("state") != "completed" for call in calls)
    total["model_calls_definition"] = "dispatch_attempts_including_unknown_outcomes"
    total["models"] = {model: totals([call for call in calls if call["model"] == model])
                       for model in sorted({call["model"] for call in calls})}
    return total
