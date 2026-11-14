"""Where the agent map puts things: boxes and link routes, worked out once
so the page only draws them and a test can check them.

Agents sit left to right by ``rank``, in columns; agents of one rank stack
in their column, those that run together first, inside a "run together"
frame. An agent whose only links lead back to an earlier agent (a detour,
such as collecting missing documents) sits under the agent it returns to.
With a router, a lane across the top holds the router's name and its
decision nodes, each above the gap after the furthest-right agent whose
result it reads; any other decision is a chip inside its agent's box.
Without a router there is one lane and every decision is a chip.

A frame is one end for every link to or from the agents in it: a decision
that starts them together sends one link to the frame, and the frame sends
one link to the decision that reads their results.

Links run on a grid: horizontal and vertical segments only, through the
gaps between boxes, never across one. The link ends on one edge of a box
are at least 24px apart. A route avoids running along another, or within
16px of one, and crossing one costs a little, so each keeps its own track.

When the columns are wider than the page can show at its smallest scale,
the ranks wrap into two rows. All sizes are in the page's pixels.

A link taken by under 10% of the runs that reach where it starts is not
drawn (the details panel lists it); a route lists the links it draws.
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from itertools import pairwise

S = 8  # grid step; every box edge and every route corner is on the grid
AW, AH = 136, 72  # agent box: a name on two lines, figures, marks
DW, DH = 136, 72  # decision box in the router lane: as tall as an agent box
CHIP = 24  # a decision chip inside an agent box, one row each
GAP = 40  # between columns: room for the links
LEFT = 24  # before the first column and after the last
STACK = 24  # between boxes stacked in one column
BACK = 40  # above a detour, for the label of its link back
MARGIN = 16
PAD = 8  # a "run together" frame's room round its boxes
LABEL = 16  # under a frame's boxes, for its "run together" label
BAR = 24  # the router's name across the top of its lane
CHANNEL = 80  # between the router lane and the agents, and between wrapped rows
BOTTOM = 24  # under the agents, for routes along the bottom
PORT = 24  # least distance between two link ends on one edge
MINOR = 0.1  # a link under this share of runs is listed, not drawn
MAX_WIDTH = 1152  # the 920px beside the details panel at 1280, at the smallest scale (0.8): wider, the ranks wrap


def _snap(v: float) -> int:
    return round(v / S) * S


def _detours(agent_graph: dict, lower: list[dict]) -> dict[str, str]:
    """Agent -> the agent it returns to, for an agent whose every link out
    leads back to an earlier agent and that runs alongside no other."""
    ids = {a["id"] for a in lower}
    out: dict[str, list[dict]] = defaultdict(list)
    for x in agent_graph["links"]:
        if x.get("from_agent") in ids:
            out[x["from_agent"]].append(x)
    found = {}
    for a in lower:
        links = out[a["id"]]
        if links and not a["alongside"] and all(x["kind"] == "direct" and x["mode"] == "return" for x in links):
            back = max(links, key=lambda x: (x.get("runs", 0), x["to_agent"]))["to_agent"]
            if back in ids and back != a["id"]:
                found[a["id"]] = back
    return {a: b for a, b in found.items() if b not in found}  # never a detour under a detour


def layout(agent_graph: dict) -> dict | None:
    """``{"width", "height", "boxes", "frames", "routes"}``, or None for one agent."""
    agents = agent_graph["agents"]
    if len(agents) < 2:
        return None
    router = agent_graph["router"]
    owner = {d["id"]: d["agent"] for d in agent_graph["decisions"]}
    in_lane = {d for d, a in owner.items() if a == router}
    chips: dict[str, list[str]] = defaultdict(list)
    for d in sorted(owner):
        if d not in in_lane:
            chips[owner[d]].append(d)
    lower = [a for a in agents if a["id"] != router]
    detour = _detours(agent_graph, lower)
    ranks = sorted({a["rank"] or 0 for a in lower if a["id"] not in detour})
    col_of_rank = {r: i for i, r in enumerate(ranks)}
    columns = len(ranks)
    per_row = columns
    if 2 * (MARGIN + LEFT) - GAP + columns * (AW + GAP) > MAX_WIDTH:
        per_row = -(-columns // 2)
    rows = -(-columns // per_row)

    top = MARGIN + (BAR + S + DH + CHANNEL if router else LABEL + S)  # without a router, room above for a side route and its label
    alongside = {a["id"]: set(a["alongside"]) for a in lower}
    height_of = {a["id"]: AH + (len(chips[a["id"]]) * CHIP + S if chips[a["id"]] else 0) for a in lower}
    col_x = lambda c: MARGIN + LEFT + (c % per_row) * (AW + GAP)

    boxes, frames = [], []
    rect: dict[str, tuple[int, int, int, int]] = {}
    frame_of: dict[str, str] = {}
    y_row = top
    for row in range(rows):
        bottom = y_row
        for c in range(row * per_row, min(columns, (row + 1) * per_row)):
            mine = [a for a in lower if a["id"] not in detour and col_of_rank[a["rank"] or 0] == c]
            together = [a for a in mine if alongside[a["id"]] & {b["id"] for b in mine}]
            alone = [a for a in mine if a not in together]
            y, x = y_row + PAD, col_x(c)
            if together:
                fid = f"together:{len(frames)}"
                y0 = y
                for a in together:
                    rect[a["id"]] = (x, y, AW, height_of[a["id"]])
                    frame_of[a["id"]] = fid
                    y += height_of[a["id"]] + STACK
                h = y - STACK + LABEL + PAD - (y0 - PAD)
                rect[fid] = (x - PAD, y0 - PAD, AW + 2 * PAD, h)
                frames.append({"id": fid, "x": x - PAD, "y": y0 - PAD, "w": AW + 2 * PAD, "h": h,
                               "agents": [a["id"] for a in together]})
                y = y0 - PAD + h + STACK
            under = lambda hosts: [d for d in lower if detour.get(d["id"]) in hosts]
            for a in under({t["id"] for t in together}) + [b for c in alone for b in [c, *under({c["id"]})]]:
                if a["id"] in detour:  # right under the agent it returns to, with room for its label
                    y += BACK - STACK
                rect[a["id"]] = (x, y, AW, height_of[a["id"]])
                y += height_of[a["id"]] + STACK
            bottom = max(bottom, y - STACK)
        y_row = bottom + CHANNEL

    # decisions in the router lane: above the gap after the furthest-right agent whose result they read
    readers: dict[str, list[str]] = defaultdict(list)
    for x in agent_graph["links"]:
        if x["kind"] == "reads" and x["from_agent"] in rect:
            readers[x["decision"]].append(x["from_agent"])
    def gap_center(d: str) -> int:
        right = [rect[a][0] + AW for a in readers[d]]
        return max(right) + GAP // 2 if right else MARGIN + LEFT // 2
    lane_y = MARGIN + BAR + S
    placed = []
    for d in sorted(in_lane, key=lambda d: (gap_center(d), d)):
        x = max(MARGIN, _snap(gap_center(d) - DW / 2), placed[-1][1] + DW + 2 * S if placed else 0)
        placed.append((d, x))
        rect[d] = (x, lane_y, DW, DH)

    width = max([r[0] + r[2] for r in rect.values()]) + LEFT + MARGIN
    height = max([r[1] + r[3] for r in rect.values()]) + BOTTOM + MARGIN
    if router:
        rect[router] = (MARGIN, MARGIN, width - 2 * MARGIN, BAR)
    width, height = _snap(width), _snap(height)

    for a in agents:
        x, y, w, h = rect[a["id"]]
        boxes.append({"id": a["id"], "kind": "router" if a["id"] == router else "agent", "x": x, "y": y, "w": w, "h": h})
        for i, d in enumerate(chips[a["id"]]):
            boxes.append({"id": d, "kind": "chip", "agent": a["id"], "x": x + S, "y": y + AH + i * CHIP, "w": w - 2 * S,
                          "h": CHIP - 4})
    for d, _ in placed:
        x, y, w, h = rect[d]
        boxes.append({"id": d, "kind": "decision", "x": x, "y": y, "w": w, "h": h})

    # the links to draw, end to end: a chip's links start or end at its agent's box, a framed agent's at its frame
    at = lambda d: d if d in in_lane else frame_of.get(owner[d], owner[d])
    end = lambda a: frame_of.get(a, a)
    pairs: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, x in enumerate(agent_graph["links"]):
        if x["kind"] == "reads":
            a, b = end(x["from_agent"]), at(x["decision"])
        elif x["kind"] == "handoff":
            a, b = at(x["decision"]), end(x["to_agent"])
        else:
            a, b = end(x["from_agent"]), end(x["to_agent"])
        if a != b and router not in (a, b) and x["share"] >= MINOR:
            pairs[(a, b)].append(i)
    framed = set(frame_of)
    obstacles = [rect[b["id"]] for b in boxes if b["kind"] != "chip" and b["id"] not in framed] + [rect[f["id"]] for f in frames]
    stacked = {(a, b) for a, b in detour.items()} | {(b, a) for a, b in detour.items()}
    routes = _route(pairs, rect, in_lane, set(detour), stacked, obstacles, width, height)
    return {"width": width, "height": height, "boxes": boxes, "frames": frames, "routes": routes}


def _side(box: tuple, other: tuple, lane: set, me: str, them: str, under: set) -> str:
    """Which edge of ``box`` a link to ``other`` leaves by: a decision in the
    router lane by its bottom, an agent to such a decision by its top, or a
    detour (whose top faces the agent above it) by its side."""
    x, y, w, h = box
    ox, oy, ow, oh = other
    if me in lane:
        return "bottom"
    if them in lane:
        return "top" if me not in under else "right" if ox + ow / 2 >= x + w / 2 else "left"
    cx = ox + ow / 2
    if oy + oh <= y and x <= cx <= x + w:
        return "top"
    if oy >= y + h and x <= cx <= x + w:
        return "bottom"
    if cx >= x + w:
        return "right"
    if cx <= x:
        return "left"
    return "top" if oy < y else "bottom"


OUT = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def _ports(pairs: dict, rect: dict, lane: set, under: set, stacked: set) -> dict:
    """A point on its box's edge for every link end, at least ``PORT`` apart
    where the edge has room. The link between a detour and the agent above
    it ends at the left of both, so its label fits to the right."""
    ends: dict[tuple[str, str], list] = defaultdict(list)  # (box, side) -> [(sort key, pair, end)]
    for (a, b) in pairs:
        for box, other, end in ((a, b, 0), (b, a, 1)):
            side = _side(rect[box], rect[other], lane, box, other, under)
            ox, oy, ow, oh = rect[other]
            key = -1 if (a, b) in stacked else oy + oh / 2 if side in ("left", "right") else ox + ow / 2
            bx, by, bw, bh = rect[box]
            far = abs(ox + ow / 2 - bx - bw / 2) if side in ("left", "right") else abs(oy + oh / 2 - by - bh / 2)
            ends[(box, side)].append((key, far, (a, b), end))
    out = {}
    for (box, side), items in ends.items():
        x, y, w, h = rect[box]
        lo, hi = (y, y + h) if side in ("left", "right") else (x, x + w)
        # by where the other box is; of two in line, the nearer takes the slot toward it and the farther goes round outside
        mid = (lo + hi) / 2
        items.sort(key=lambda t: (t[0], t[1] if t[0] <= mid else -t[1], t[2], t[3]))
        items = [(key, pair, end) for key, _, pair, end in items]
        n = len(items)
        step = PORT if (hi - lo - 4 * S) // PORT + 1 >= n else S
        slots = list(range(lo + 2 * S, hi - 2 * S + 1, step)) or [_snap((lo + hi) / 2)]
        at = [0 if items[0][0] == -1 and n == 1 else
              min(len(slots) - 1, round((i + 0.5) * len(slots) / n - 0.5)) if n <= len(slots) else i % len(slots)
              for i in range(n)]
        for i, (key, pair, end) in enumerate(items):
            v = slots[at[i]]
            point = {"left": (x, v), "right": (x + w, v), "top": (v, y), "bottom": (v, y + h)}[side]
            out[(pair, end)] = (point, side)
    # an end alone on its side moves level with the end it faces, where its edge allows, so that link runs straight
    alone = {(box, side) for (box, side), items in ends.items() if len(items) == 1}
    facing = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
    for pair in pairs:
        if pair in stacked:  # keeps the left, for its label
            continue
        for end in (0, 1):
            (p, side), ((q, other_side)) = out[(pair, end)], out[(pair, 1 - end)]
            box = pair[end]
            if (box, side) not in alone or facing[side] != other_side:
                continue
            x, y, w, h = rect[box]
            along = 1 if side in ("left", "right") else 0
            lo, hi = (y, y + h) if along else (x, x + w)
            if lo + 2 * S <= q[along] <= hi - 2 * S:
                out[(pair, end)] = ((p[0], q[1]) if along else (q[0], p[1]), side)
                break
    return out


def _route(pairs: dict, rect: dict, lane: set, under: set, stacked: set, obstacles: list, width: int,
           height: int) -> list[dict]:
    cols, rows = width // S + 1, height // S + 1
    blocked = set()
    near = set()
    for x, y, w, h in obstacles:
        for i in range(x // S, (x + w) // S + 1):
            for j in range(y // S, (y + h) // S + 1):
                blocked.add((i, j))
        for i in range(x // S - 1, (x + w) // S + 2):
            near.update({(i, y // S - 1), (i, (y + h) // S + 1)})
        for j in range(y // S - 1, (y + h) // S + 2):
            near.update({(x // S - 1, j), ((x + w) // S + 1, j)})
    used: dict[tuple, set] = defaultdict(set)  # grid point -> axes routes already use there
    close: dict[tuple, set] = defaultdict(set)  # grid point -> axes routes use beside it
    ports = _ports(pairs, rect, lane, under, stacked)
    routes = []
    order = sorted(pairs, key=lambda p: (abs(rect[p[0]][0] - rect[p[1]][0]) + abs(rect[p[0]][1] - rect[p[1]][1]), p))
    for pair in order:
        (p0, s0), (p1, s1) = ports[(pair, 0)], ports[(pair, 1)]
        start = (p0[0] // S + OUT[s0][0], p0[1] // S + OUT[s0][1])
        goal = (p1[0] // S + OUT[s1][0], p1[1] // S + OUT[s1][1])
        cells = _astar(start, goal, OUT[s0], (-OUT[s1][0], -OUT[s1][1]), blocked, near, used, close, cols, rows)
        points = [p0] + [(i * S, j * S) for i, j in cells] + [p1]
        for (i0, j0), (i1, j1) in pairwise(cells):
            axis = "h" if j0 == j1 else "v"
            used[(i0, j0)].add(axis)
            used[(i1, j1)].add(axis)
            for k in (-2, -1, 1, 2):  # the grid lines beside it, 8 and 16px away: another route keeps off them
                for i, j in ((i0, j0), (i1, j1)):
                    close[(i, j + k) if axis == "h" else (i + k, j)].add(axis)
        routes.append({"from": pair[0], "to": pair[1], "links": pairs[pair], "points": [list(p) for p in _corners(points)]})
    routes.sort(key=lambda r: (r["from"], r["to"]))
    return routes


def _astar(start, goal, first, last, blocked, near, used, close, cols, rows) -> list:
    """Grid points from ``start`` to ``goal``: leaving in direction ``first``,
    arriving moving in direction ``last``, with as few bends as it can."""
    BEND, ALONG, CROSS, NEAR, BESIDE = 10, 40, 2, 1, 6
    frontier = [(0, 0, start, first)]
    cost = {(start, first): 0}
    came = {}
    while frontier:
        _, c, p, d = heapq.heappop(frontier)
        if p == goal and d == last:
            break
        if c > cost.get((p, d), float("inf")):
            continue
        for nd in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if nd == (-d[0], -d[1]):
                continue
            q = (p[0] + nd[0], p[1] + nd[1])
            if not (0 <= q[0] < cols and 0 <= q[1] < rows) or q in blocked:
                continue
            axis = "h" if nd[1] == 0 else "v"
            step = 1 + (BEND if nd != d else 0) + (NEAR if q in near else 0)
            if used.get(q):
                step += ALONG if axis in used[q] else CROSS
            elif axis in close.get(q, ()):
                step += BESIDE
            nc = c + step
            if nc < cost.get((q, nd), float("inf")):
                cost[(q, nd)] = nc
                came[(q, nd)] = (p, d)
                h = abs(q[0] - goal[0]) + abs(q[1] - goal[1])
                heapq.heappush(frontier, (nc + h, nc, q, nd))
    state = (goal, last)
    if state not in came and goal != start:  # no way round: go straight (never expected on this layout)
        return [start, goal]
    path = [goal]
    while state in came:
        state = came[state]
        path.append(state[0])
    return path[::-1]


def _corners(points: list) -> list:
    out = [points[0]]
    for prev, here, nxt in zip(points, points[1:], points[2:]):
        if (prev[0] == here[0] == nxt[0]) or (prev[1] == here[1] == nxt[1]):
            continue
        out.append(here)
    out.append(points[-1])
    return out
