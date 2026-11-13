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
