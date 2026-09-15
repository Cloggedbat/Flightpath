"""Shot and session records.

Deliberately the same shape as OpenFlight's cloud sync payload: kilobyte-scale
summaries, no raw signal. If this ever grows a backend, the schema is already
the right one.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

from . import physics


@dataclass
class Shot:
    timestamp: str
    club: str | None
    ball_speed_mph: float
    launch_angle_deg: float
    carry_yards: float
    apex_feet: float
    hang_time_s: float
    descent_angle_deg: float
    backspin_rpm: float
    spin_is_estimated: bool
    confidence: float
    n_frames: int
    residual_px: float
    fps_used: float
    px_per_m: float
    source_clip: str

    @classmethod
    def from_track(cls, track, scale, club=None, source_clip="", backspin_rpm=None):
        speed_ms = scale.px_s_to_ms(track.speed_px_s)
        angle = track.launch_angle_deg
        estimated = backspin_rpm is None
        spin = backspin_rpm if backspin_rpm is not None else \
            physics.estimate_backspin_rpm(speed_ms, angle)
        launch = physics.Launch(speed_ms, angle, spin)
        flight = physics.simulate(launch)
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            club=club,
            ball_speed_mph=round(physics.ms_to_mph(speed_ms), 1),
            launch_angle_deg=round(angle, 1),
            carry_yards=round(flight.carry_yards, 1),
            apex_feet=round(flight.apex_feet, 1),
            hang_time_s=round(flight.hang_time_s, 2),
            descent_angle_deg=round(flight.descent_angle_deg, 1),
            backspin_rpm=round(spin),
            spin_is_estimated=estimated,
            confidence=round(track.confidence, 2),
            n_frames=track.n_frames,
            residual_px=round(track.residual_px, 2),
            fps_used=track.fps,
            px_per_m=round(scale.px_per_m, 1),
            source_clip=source_clip,
        )


@dataclass
class Session:
    shots: list[Shot] = field(default_factory=list)
    started: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def add(self, shot: Shot) -> None:
        self.shots.append(shot)

    def by_club(self) -> dict[str, list[Shot]]:
        out: dict[str, list[Shot]] = {}
        for s in self.shots:
            out.setdefault(s.club or "unknown", []).append(s)
        return out

    def club_summary(self) -> list[dict]:
        """Gapping table: the thing you actually go to the range for."""
        rows = []
        for club, shots in self.by_club().items():
            carries = [s.carry_yards for s in shots]
            speeds = [s.ball_speed_mph for s in shots]
            rows.append({
                "club": club,
                "shots": len(shots),
                "carry_avg": round(statistics.mean(carries), 1),
                "carry_sd": round(statistics.pstdev(carries), 1) if len(carries) > 1 else 0.0,
                "carry_min": round(min(carries), 1),
                "carry_max": round(max(carries), 1),
                "ball_speed_avg": round(statistics.mean(speeds), 1),
            })
        rows.sort(key=lambda r: r["carry_avg"], reverse=True)
        return rows

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "started": self.started,
                "shots": [asdict(s) for s in self.shots],
                "club_summary": self.club_summary(),
            },
            indent=indent,
        )

    def save(self, path: str) -> str:
        with open(path, "w") as fh:
            fh.write(self.to_json())
        return path

    @classmethod
    def load(cls, path: str) -> "Session":
        with open(path) as fh:
            raw = json.load(fh)
        sess = cls(started=raw.get("started", ""))
        for s in raw.get("shots", []):
            sess.shots.append(Shot(**s))
        return sess
