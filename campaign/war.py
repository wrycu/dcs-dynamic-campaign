"""Run a scenario's whole war with no DCS: the ground war, both sides' planners and the air war.

    python3 -m campaign.war southern_syria --hours 168 [--seed 2] [--quiet]

Prints a line per day (ground held, vehicles left, sorties, aircraft lost and why, vehicles killed
from the air), and writes ``build/war/<scenario>-air-<hours>h.png`` and ``.log``.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

from .air import AirSpawner
from .air_bubble import AirBubble
from .air_war import AirWar
from .bubble import BubbleConfig, strength
from .ground_war import MANEUVER, GroundWar, sides_from
from .model import FlightState, GroundRole, Side, SpawnState
from .planner import Planner


@dataclass
class War:
    """Everything that runs the war, wired as the campaign server will wire it."""
    state: object
    ground: GroundWar
    air: AirWar
    planner: Planner
    bubble: AirBubble
    spawner: AirSpawner

    def step(self, dt: float) -> None:
        st = self.state
        t0 = st.clock
        # Due flights take off (no players: none of them in DCS).
        for fid in self.bubble.plan().launch_virtual:
            f = st.flights[fid]
            try:
                self.spawner.launch_aggregated(f, self.bubble.base_pos(st.squadrons[f.squadron_id]))
            except ValueError:
                pass   # no aircraft: the air war cancels it once it's late
        self.bubble.advance(dt)   # the clock, and the flights
        t1 = st.clock
        self.ground.advance(t0, t1)
        self.air.advance(t0, t1)
        self.planner.advance(t0, t1)


def build(sd, built, seed: int = 1, notify=None) -> War:
    st = built.state
    sides = sides_from(sd)
    postures = {s: o.air_posture() for s, o in sides.items()}
    ground = GroundWar(st, built.theater, sides, seed=seed, notify=notify, in_play=built.in_play)
    air = AirWar(st, built.theater, postures, seed=seed, notify=notify)
    planner = Planner(st, built.theater, postures, ground, air, seed=seed)

    async def no_dcs(op, data):   # pragma: no cover - nothing is spawned offline
        raise RuntimeError("no DCS")

    spawner = AirSpawner(st, no_dcs)
    objs = built.theater.objectives
    bubble = AirBubble(st, spawner, BubbleConfig(), base_pos=lambda sq: objs[sq.base_id].pos if sq.base_id in objs
                       else None, takeoff_for=no_dcs)
    bubble.on_waypoint = air.on_waypoint
    return War(st, ground, air, planner, bubble, spawner)


def main() -> None:
    from .scenario_file import ROOT, build_state, load_scenario_def, scenario_path

    ap = argparse.ArgumentParser(description="Run a scenario's war (ground and air) with no DCS.")
    ap.add_argument("name")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--quiet", action="store_true", help="only the daily summary")
    a = ap.parse_args()
    sd = load_scenario_def(scenario_path(a.name))
    built = build_state(sd)
    st = built.state
    events: list[str] = []

    def stamp() -> str:
        return f"T+{int(st.clock // 3600):3d}h{int(st.clock % 3600 // 60):02d}"

    def note(text: str) -> None:
        events.append(f"{stamp()}  {text}")
        if not a.quiet:
            print(f"{stamp()}  {text}", flush=True)

    class Collect(logging.Handler):
        def emit(self, rec: logging.LogRecord) -> None:
            msg = rec.getMessage()
            if msg.startswith(("AIR", "PLAN")):
                events.append(f"{stamp()}  {msg}")

    root = logging.getLogger("campaign")
    root.setLevel(logging.INFO)
    root.addHandler(Collect())
    root.propagate = False

    war = build(sd, built, a.seed, note)
    launched: dict[Side, Counter] = defaultdict(Counter)
    seen: set[str] = set()
    for _ in range(int(a.hours * 60)):
        war.step(60.0)
        for f in st.flights.values():
            if f.id not in seen and f.state is not FlightState.PLANNED:
                seen.add(f.id)
                launched[st.squadrons[f.squadron_id].side][f.mission] += f.aircraft
        if st.clock % 86400 == 0:
            day = int(st.clock // 86400)
            held = {s: sum(1 for o in st.objectives.values() if o.owner is s) for s in (Side.BLUE, Side.RED)}
            veh = {s: sum(strength(u) for u in war.ground.units(s) if u.role in MANEUVER) for s in (Side.BLUE, Side.RED)}
            lost = {s: Counter(c for t, x, c in war.air.losses if x is s and t > st.clock - 86400)
                    for s in (Side.BLUE, Side.RED)}
            sorties = {s: sum(launched[s].values()) for s in (Side.BLUE, Side.RED)}
            ready = {s: sum(q.aircraft_ready for q in st.squadrons.values() if q.side is s) for s in (Side.BLUE, Side.RED)}
            sams = sum(1 for _ in war.air.sam_sites(Side.RED))
            print(f"--- day {day}: blue holds {held[Side.BLUE]}, red {held[Side.RED]}; manoeuvre vehicles blue "
                  f"{veh[Side.BLUE]}, red {veh[Side.RED]}; battles {len(st.battles)}; red SAM sites up {sams}", flush=True)
            for s in (Side.BLUE, Side.RED):
                print(f"    {s.name.lower()}: {sorties[s]} sorties ({dict(launched[s])}); lost "
                      f"{sum(lost[s].values())} {dict(lost[s])}; ready {ready[s]}; vehicles killed from the air "
                      f"{war.air.killed[s]}", flush=True)
            launched = defaultdict(Counter)
    from tools.draw_scenario import draw
    path = ROOT / "build" / "war" / f"{sd.name}-air-{int(a.hours)}h.png"
    draw(path, sd, built, title=f"{sd.title}: T+{int(a.hours)}h with the air war (seed {a.seed})")
    out = ROOT / "build" / "war" / f"{sd.name}-air-{int(a.hours)}h.log"
    out.write_text("\n".join(events) + "\n")
    print(f"picture: {path}\nevents: {out}")


if __name__ == "__main__":
    sys.exit(main())
