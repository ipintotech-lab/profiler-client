"""The agent map's positions: nothing overlaps, no link crosses a box, link
ends on one edge are at least 24px apart, and the map fits beside its details
panel at 1280 by 800 (scaled to 0.8 at most and wrapped into two rows when the
ranks are too many)."""
from collections import Counter
from itertools import pairwise

from profiler_client.agent_map import layout


def _graph(agents, decisions=(), links=(), router=None):
    return {"router": router,
            "agents": [{"id": a, "rank": r, "alongside": list(w)} for a, r, w in agents],
            "decisions": [{"id": d, "agent": a} for d, a in decisions],
            "links": list(links)}


def _reads(a, d, share=1.0):
    return {"kind": "reads", "from_agent": a, "decision": d, "share": share}


def _hand(d, a, share=1.0, mode="either"):
    return {"kind": "handoff", "decision": d, "to_agent": a, "share": share, "mode": mode}


def _direct(a, b, share=1.0, mode="either"):
    return {"kind": "direct", "from_agent": a, "to_agent": b, "share": share, "mode": mode}


def _loan(extra=0):
    """The loan-application shape, with ``extra`` more specialists after pricing."""
    tail = [f"extra-{i}" for i in range(extra)]
    agents = [("sup", None, ()), ("kyc", 0, ()), ("credit", 1, ("fraud",)), ("fraud", 1, ("credit",)),
              ("docs", 1, ()), ("pricing", 2, ())] + [(t, 3 + i, ()) for i, t in enumerate(tail)] + [
              ("compliance", 3 + extra, ()), ("letter", 4 + extra, ())]
    decisions = [("after_kyc", "sup"), ("after_checks", "sup"), ("after_pricing", "sup"), ("after_compliance", "sup")]
    links = [_reads("kyc", "after_kyc"), _hand("after_kyc", "credit", mode="both"), _hand("after_kyc", "fraud", mode="both"),
             _hand("after_kyc", "docs", 0.32), _direct("docs", "kyc", mode="return"),
             _reads("credit", "after_checks"), _reads("fraud", "after_checks"),
             _hand("after_checks", "pricing", 0.8), _hand("after_checks", "letter", 0.19),
             _reads("pricing", "after_pricing"), _hand("after_pricing", "letter", 0.79),
             _reads("compliance", "after_compliance"), _hand("after_compliance", "letter")]
    chain = ["pricing", *tail, "compliance"]
    links += [_direct(a, b) for a, b in pairwise(chain[1:])]
    links.append(_hand("after_pricing", chain[1], 0.21))
    return _graph(agents, decisions, links, router="sup")


def _problems(lay):
    boxes = [b for b in lay["boxes"] if b["kind"] != "chip"]
    out = []
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]:
                out.append(("overlap", a["id"], b["id"]))
    ends = Counter()
    for r in lay["routes"]:
        pts = r["points"]
        ends[tuple(pts[0])] += 1
        ends[tuple(pts[-1])] += 1
        for (x0, y0), (x1, y1) in pairwise(pts):
            if x0 != x1 and y0 != y1:
                out.append(("diagonal", r["from"], r["to"]))
            lx, hx, ly, hy = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            for b in boxes:  # the segment's line meets the box's inside
                across = b["x"] < lx < b["x"] + b["w"] if lx == hx else lx < b["x"] + b["w"] and b["x"] < hx
                down = b["y"] < ly < b["y"] + b["h"] if ly == hy else ly < b["y"] + b["h"] and b["y"] < hy
                if across and down:
                    out.append(("crosses", r["from"], r["to"], b["id"]))
            if not (0 <= lx and hx <= lay["width"] and 0 <= ly and hy <= lay["height"]):
                out.append(("outside", r["from"], r["to"]))
    out += [("shared end", p) for p, n in ends.items() if n > 1]
    # link ends on one edge of a box: at least 24px apart (every edge here has room for that)
    sides = {}
    for b in [*boxes, *lay["frames"]]:
        for r in lay["routes"]:
            for x, y in (r["points"][0], r["points"][-1]):
                for side, on, v in (("top", y == b["y"], x), ("bottom", y == b["y"] + b["h"], x),
                                    ("left", x == b["x"], y), ("right", x == b["x"] + b["w"], y)):
                    lo, hi = (b["x"], b["x"] + b["w"]) if side in ("top", "bottom") else (b["y"], b["y"] + b["h"])
                    if on and lo < v < hi:
                        sides.setdefault((b.get("id"), side), []).append(v)
    for key, vs in sides.items():
        vs.sort()
        out += [("ends closer than 24px", key) for u, v in pairwise(vs) if v - u < 24]
    return out


def test_one_agent_has_no_agent_map():
    assert layout(_graph([("solo", 0, ())])) is None


def test_the_loan_application_shape_fits_one_screen_with_clean_links():
    lay = layout(_loan())
    assert _problems(lay) == []
    assert lay["width"] <= 960 and lay["height"] <= 440  # beside a details panel at 1280 by 800
    kinds = Counter(b["kind"] for b in lay["boxes"])
    assert kinds == {"router": 1, "agent": 7, "decision": 4}
    box = {b["id"]: b for b in lay["boxes"]}
    # ranks run left to right; the router's decisions sit in its lane, above the agents
    assert box["kyc"]["x"] < box["credit"]["x"] < box["pricing"]["x"] < box["compliance"]["x"] < box["letter"]["x"]
    assert box["credit"]["x"] == box["fraud"]["x"]
    # docs only hands back to kyc: it sits right under kyc, joined by one straight link up
    assert box["docs"]["x"] == box["kyc"]["x"] and box["docs"]["y"] > box["kyc"]["y"] + box["kyc"]["h"]
    back = next(r for r in lay["routes"] if (r["from"], r["to"]) == ("docs", "kyc"))
    assert len(back["points"]) == 2 and back["points"][0][0] == back["points"][1][0] < box["kyc"]["x"] + box["kyc"]["w"] / 2
    assert max(box[d]["y"] + box[d]["h"] for d in ("after_kyc", "after_checks")) < box["kyc"]["y"]
    # the decision after kyc sits between kyc and the checks
    assert box["kyc"]["x"] + box["kyc"]["w"] < box["after_kyc"]["x"] + box["after_kyc"]["w"] / 2 < box["credit"]["x"]
    # credit and fraud run together: one frame, one link into it from the decision that starts them,
    # one link out of it to the decision that reads their results
    assert [f["agents"] for f in lay["frames"]] == [["credit", "fraud"]]
    frame = lay["frames"][0]["id"]
    pairs = {(r["from"], r["to"]): len(r["links"]) for r in lay["routes"]}
    assert pairs[("after_kyc", frame)] == pairs[(frame, "after_checks")] == 2
    assert not {"credit", "fraud"} & {e for p in pairs for e in p}
    # one route per pair of ends, none to the router itself
    assert len(lay["routes"]) == len(pairs) == 12


def test_ten_agents_wrap_into_two_rows_and_fit_at_the_smallest_scale():
    lay = layout(_loan(extra=3))
    assert sum(b["kind"] == "agent" for b in lay["boxes"]) == 10
    assert _problems(lay) == []
    assert lay["width"] * 0.8 <= 920 and lay["height"] * 0.8 <= 720  # beside the details panel
    rows = {b["y"] for b in lay["boxes"] if b["kind"] == "agent" and b["id"] not in ("fraud", "docs")}
    assert len(rows) == 2


def test_a_pipeline_with_no_router_is_one_lane():
    names = [f"stage-{i}" for i in range(5)]
    g = _graph([(a, i, ()) for i, a in enumerate(names)],
               links=[_direct(a, b) for a, b in pairwise(names)] + [_direct(names[3], names[1], 0.4)])
    lay = layout(g)
    assert _problems(lay) == []
    assert {b["kind"] for b in lay["boxes"]} == {"agent"}
    assert len({b["y"] for b in lay["boxes"]}) == 1
    assert lay["width"] <= 1280 and lay["height"] <= 720


def test_a_decision_owned_by_a_non_router_agent_is_a_chip_in_its_box():
    g = _graph([("a", 0, ()), ("b", 1, ()), ("c", 1, ())], [("pick", "a")],
               [_hand("pick", "b", 0.6), _hand("pick", "c", 0.4)])
    lay = layout(g)
    chip = next(b for b in lay["boxes"] if b["kind"] == "chip")
    owner = next(b for b in lay["boxes"] if b["id"] == "a")
    assert chip["id"] == "pick" and chip["agent"] == "a"
    assert owner["x"] < chip["x"] and chip["x"] + chip["w"] < owner["x"] + owner["w"]
    assert owner["y"] < chip["y"] and chip["y"] + chip["h"] < owner["y"] + owner["h"]
    assert {(r["from"], r["to"]) for r in lay["routes"]} == {("a", "b"), ("a", "c")}
    assert _problems(lay) == []


def test_a_route_taken_by_under_ten_percent_of_runs_is_not_drawn():
    g = _loan()
    g["links"].append(_hand("after_kyc", "letter", 0.05))
    lay = layout(g)
    assert ("after_kyc", "letter") not in {(r["from"], r["to"]) for r in lay["routes"]}
