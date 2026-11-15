"""The workflow graph is drawn from parent -> event_id links only."""
from profiler_client.contract import TraceEvent
from profiler_client.graph import build_graph


def _ev(run, eid, node, parent=None, cost=0.0, workflow="wf"):
    return TraceEvent(trace_id="t", run_id=run, workflow=workflow, agent="a", event_type="tool_call",
                      node=node, event_id=eid, parent=parent, cost=cost)


def test_a_branch_gives_the_expected_counts():
    # run 1: llm chose A and B; run 2: llm chose A; a step in another workflow is ignored
    events = [
        _ev("r1", "e1", "llm", cost=1.0), _ev("r1", "e2", "A", "e1"), _ev("r1", "e3", "B", "e1"),
        _ev("r2", "e4", "llm", cost=2.0), _ev("r2", "e5", "A", "e4"),
        _ev("r3", "x1", "other", "e1", workflow="wf2"),
    ]
    out = build_graph(events, "wf", "invokes")
    assert out["availability"] == {"available": True}
    graph = out["graph"]
    assert graph["relation"] == "invokes" and graph["run_count"] == 2
    assert {n["id"]: (n["occurrences"], n["cost"]) for n in graph["nodes"]} == {
        "llm": (2, 3.0), "A": (2, 0.0), "B": (1, 0.0)}
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    assert set(edges) == {("llm", "A"), ("llm", "B")}
    assert edges[("llm", "A")]["transition_count"] == 2 and edges[("llm", "A")]["distinct_run_count"] == 2
    assert edges[("llm", "A")]["evidence_sample"] == [{"run_id": "r1", "event_id": "e2"},
                                                     {"run_id": "r2", "event_id": "e5"}]


def test_event_order_alone_draws_no_edge():
    # three steps in a row, no parents: order is not evidence of a link
    events = [_ev("r1", f"e{i}", n) for i, n in enumerate("ABC")]
    assert build_graph(events, "wf", "invokes")["availability"] == {"available": False, "code": "NO_PARENT_LINKS"}


def test_contract_v1_data_has_no_graph():
    events = [_ev("r1", None, "llm"), _ev("r1", None, "A", "m1")]
    assert build_graph(events, "wf", None) == {"availability": {"available": False, "code": "NO_EVENT_IDS"}}


def test_what_an_llm_call_read_and_whether_it_replied_close_the_loop():
    def llm(run, eid, trigger, parent=None):
        return TraceEvent(trace_id="t", run_id=run, workflow="wf", agent="a", event_type="decision",
                          node=f"llm_call ← {trigger.removesuffix(' (error)')}", event_id=eid, parent=parent, model="m",
                          trigger=trigger, cost=1.0)
    # prompt -> llm chooses A -> llm reads A's result, chooses A again -> llm reads it, replies
    events = [
        llm("r1", "l1", "user_prompt"), _ev("r1", "t1", "A", "l1"),
        llm("r1", "l2", "A"), _ev("r1", "t2", "A", "l2"),
        llm("r1", "l3", "A (error)"),
    ]
    graph = build_graph(events, "wf", "invokes")["graph"]
    edges = {(e["from"], e["to"]): (e["kind"], e["transition_count"]) for e in graph["edges"]}
    assert edges == {
        ("@prompt", "llm_call ← user_prompt"): ("starts", 1),
        ("llm_call ← user_prompt", "A"): ("invokes", 1),
        ("A", "llm_call ← A"): ("reads_result", 2),
        ("llm_call ← A", "A"): ("invokes", 1),
        ("llm_call ← A", "@reply"): ("ends", 1),
    }
    # with links of unknown meaning, no run is said to end in a reply
    unspecified = build_graph(events, "wf", "unspecified")["graph"]
    assert all(e["kind"] != "ends" for e in unspecified["edges"])


def _agent_ev(run, eid, agent, node, parent=None):
    return TraceEvent(trace_id="t", run_id=run, workflow="wf", agent=agent, event_type="tool_call",
                      node=node, event_id=eid, parent=parent)


def test_each_agent_gets_its_own_box_and_handoffs_say_fork_branch_or_loop():
    # r1, r2: the router starts fraud and credit together (both look up the balance), then letter
    # r3: the router starts docs alone, docs hands back to kyc, which ran before (a loop), then letter
    events = []
    for run in ("r1", "r2"):
        events += [_agent_ev(run, f"{run}k", "kyc", "kyc_lookup"), _agent_ev(run, f"{run}r", "sup", "route"),
                   _agent_ev(run, f"{run}f", "fraud", "balance", f"{run}r"),
                   _agent_ev(run, f"{run}c", "credit", "balance", f"{run}r"),
                   _agent_ev(run, f"{run}s", "credit", "score", f"{run}c")]
    events += [_agent_ev("r3", "k1", "kyc", "kyc_lookup"), _agent_ev("r3", "r", "sup", "route"),
               _agent_ev("r3", "d", "docs", "validate", "r"), _agent_ev("r3", "k2", "kyc", "kyc_lookup", "d")]
    graph = build_graph(events, "wf", "invokes")["graph"]
    assert graph["version"] == 3
    nodes = {n["id"]: n for n in graph["nodes"]}
    # the balance lookup is one box per agent; a step only one agent uses keeps its name
    assert nodes["balance @ fraud"]["agent"] == "fraud" and nodes["balance @ fraud"]["step"] == "balance"
    assert nodes["balance @ credit"]["occurrences"] == 2 and "step" not in nodes["score"]
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    assert edges[("balance @ credit", "score")].keys().isdisjoint({"fork", "loop"})  # within one agent
    assert edges[("route", "balance @ fraud")]["fork"] == {"mode": "both", "share_of_runs": 0.667}
    assert edges[("route", "balance @ credit")]["fork"] == {"mode": "both", "share_of_runs": 0.667}
    assert edges[("route", "validate")]["fork"] == {"mode": "either", "share_of_runs": 0.333}
    assert edges[("validate", "kyc_lookup")]["loop"] == {"runs": 1, "share_of_runs": 0.333, "passes": 1.0}
    assert "loop" not in edges[("route", "validate")]  # docs had not run before


def _decision(run, eid, node, trigger, cost=1.0):
    return TraceEvent(trace_id="t", run_id=run, workflow="wf", agent="sup", event_type="decision", node=node,
                      event_id=eid, model="m", trigger=trigger, cost=cost)


def test_the_agent_graph_has_one_link_per_agent_pair_and_ranks_from_the_start():
    # kyc looks up; the router hands to fraud and credit together, or (r3) to docs, which hands back to kyc;
    # after the checks the router reads both results and hands to the letter
    events = []
    for run in ("r1", "r2", "r3"):
        events += [_agent_ev(run, f"{run}k", "kyc", "kyc_lookup"), _decision(run, f"{run}d1", "route_kyc", "kyc_lookup")]
        if run == "r3":
            events += [_agent_ev(run, f"{run}v", "docs", "validate", f"{run}d1"),
                       _agent_ev(run, f"{run}k2", "kyc", "kyc_lookup", f"{run}v"),
                       _decision(run, f"{run}d1b", "route_kyc", "kyc_lookup")]
            d = f"{run}d1b"
        else:
            d = f"{run}d1"
        events += [_agent_ev(run, f"{run}f", "fraud", "balance", d), _agent_ev(run, f"{run}r", "fraud", "risk", d),
                   _agent_ev(run, f"{run}c", "credit", "balance", d), _agent_ev(run, f"{run}s", "credit", "score", d),
                   _decision(run, f"{run}d2", "route_checks", "risk + score", cost=3.0),
                   _agent_ev(run, f"{run}l", "letter", "send", f"{run}d2")]
    ag = build_graph(events, "wf", "invokes")["graph"]["agent_graph"]
    assert ag["router"] == "sup"
    agents = {a["id"]: a for a in ag["agents"]}
    assert [a["id"] for a in ag["agents"]][:2] == ["sup", "kyc"]  # the router first, then run order
    assert {a: x["rank"] for a, x in agents.items()} == {
        "sup": None, "kyc": 0, "docs": 1, "fraud": 1, "credit": 1, "letter": 2}
    assert agents["fraud"]["alongside"] == ["credit"] and agents["docs"]["alongside"] == []
    assert agents["sup"]["share"] == 1.0 and agents["kyc"]["runs"] == 3 and agents["fraud"]["steps"] == 2
    assert {d["id"]: (d["calls"], d["cost"], d["share"]) for d in ag["decisions"]} == {
        "route_kyc": (4, 4.0, 0.308), "route_checks": (3, 9.0, 0.692)}
    links = {(x["kind"], x.get("from_agent") or x.get("decision"), x.get("to_agent") or x.get("decision")): x
             for x in ag["links"]}
    # one link per pair, and never to a single tool
    assert set(links) == {
        ("reads", "kyc", "route_kyc"), ("reads", "fraud", "route_checks"), ("reads", "credit", "route_checks"),
        ("handoff", "route_kyc", "fraud"), ("handoff", "route_kyc", "credit"), ("handoff", "route_kyc", "docs"),
        ("handoff", "route_checks", "letter"), ("direct", "docs", "kyc")}
    assert (links[("handoff", "route_kyc", "fraud")]["mode"], links[("handoff", "route_kyc", "fraud")]["share"]) == (
        "both", 1.0)
    assert links[("handoff", "route_kyc", "docs")]["mode"] == "either"
    assert links[("direct", "docs", "kyc")]["mode"] == "return"
    assert links[("direct", "docs", "kyc")]["loop"] == {"runs": 1, "share_of_runs": 0.333, "passes": 1.0}


def test_a_pipeline_with_no_llm_handoffs_has_no_router_and_ranks_in_order():
    events = []
    for run in ("r1", "r2"):
        events += [_agent_ev(run, f"{run}a", "intake", "read"), _agent_ev(run, f"{run}b", "check", "verify", f"{run}a"),
                   _agent_ev(run, f"{run}c", "reply", "send", f"{run}b")]
    ag = build_graph(events, "wf", "invokes")["graph"]["agent_graph"]
    assert ag["router"] is None and ag["decisions"] == []
    assert [(a["id"], a["rank"]) for a in ag["agents"]] == [("intake", 0), ("check", 1), ("reply", 2)]
    assert {x["mode"] for x in ag["links"]} == {"either"} and {x["kind"] for x in ag["links"]} == {"direct"}
