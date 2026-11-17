"""Save/load for the in-game demos: load at startup, autosave while running, save on exit.

    ap = argparse.ArgumentParser(); demo_save.add_args(ap, "bubble")
    saver = demo_save.DemoSave(args.save, fresh=args.new)
    snap = saver.load()                       # None: no save, or --new
    ... build the demo from snap.state / snap.extra ...
    async with saver.running(bridge, lambda: (state, {"ground": spawner.export()})):
        await forever
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from campaign.bridge import Bridge
from campaign.model import CampaignState
from campaign.persist import Autosave, SaveError, Snapshot, load
from campaign.protocol import Src

log = logging.getLogger(__name__)

INTERVAL_S = 5.0


def add_args(ap: argparse.ArgumentParser, name: str) -> None:
    ap.add_argument("--save", type=Path, default=Path(f"build/saves/{name}.json"),
                    help="campaign save file: loaded at startup, written while running (default %(default)s)")
    ap.add_argument("--new", action="store_true",
                    help="start a fresh campaign; the old save is kept as <save>.1 once this one saves")


class DemoSave:
    def __init__(self, path: Path, fresh: bool = False) -> None:
        self.path = path
        self.fresh = fresh
        self.snap: Snapshot | None = None

    def load(self) -> Snapshot | None:
        if self.fresh or not self.path.exists():
            print(f"SAVE    new campaign; saving to {self.path}", flush=True)
            return None
        try:
            self.snap = load(self.path)
        except SaveError as e:
            raise SystemExit(f"can't load the save: {e}\n(fix it, pick an older {self.path.name}.N with "
                             f"--save, or start over with --new)")
        print(f"SAVE    loaded {self.path} (saved {self.snap.saved_at})", flush=True)
        return self.snap

    def restore_runs(self, bridge: Bridge) -> None:
        """Seed the bridge so a mission that's still running resumes instead of counting as new."""
        for src, (run_id, seq) in (self.snap.runs if self.snap else {}).items():
            bridge.restore_run(src, run_id, seq)

    @contextlib.asynccontextmanager
    async def running(self, bridge: Bridge,
                      content: Callable[[], tuple[CampaignState, dict[str, Any]]]) -> AsyncIterator[None]:
        def snapshot() -> Snapshot:
            state, extra = content()
            runs = {src: run for src in (Src.MISSION, Src.HOOKS) if (run := bridge.run_state(src))}
            return Snapshot(state, runs=runs, extra=extra)

        auto = Autosave(self.path, snapshot)

        async def loop() -> None:
            while True:
                await asyncio.sleep(INTERVAL_S)
                try:
                    auto.maybe_save()
                except (SaveError, OSError) as e:
                    log.error("autosave failed: %s", e)

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                auto.save_now()
                print(f"SAVE    saved {self.path}", flush=True)
            except (SaveError, OSError) as e:
                log.error("final save failed: %s", e)
