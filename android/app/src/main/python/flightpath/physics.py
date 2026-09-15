"""Ball flight model.

Same job as OpenFlight's ballistic stage: turn launch conditions into a carry
number. Speed and launch angle are measured; spin is estimated unless supplied.

Units are SI internally. Conversion helpers at the bottom.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Physical constants
G = 9.80665                 # m/s^2
RHO = 1.225                 # kg/m^3, sea level standard air
BALL_MASS = 0.04593         # kg, USGA max
BALL_RADIUS = 0.021335      # m, USGA min diameter 1.68 in
BALL_AREA = math.pi * BALL_RADIUS ** 2

MPH_PER_MS = 2.2369362920544
YARDS_PER_M = 1.0936132983377


@dataclass
class Launch:
    """Measured launch conditions."""
    ball_speed_ms: float
    launch_angle_deg: float
    backspin_rpm: float
    azimuth_deg: float = 0.0

    @property
    def ball_speed_mph(self) -> float:
        return self.ball_speed_ms * MPH_PER_MS


@dataclass
class Flight:
    carry_m: float
    apex_m: float
    hang_time_s: float
    descent_angle_deg: float

    @property
    def carry_yards(self) -> float:
        return self.carry_m * YARDS_PER_M

    @property
    def apex_feet(self) -> float:
        return self.apex_m * 3.280839895


def estimate_backspin_rpm(ball_speed_ms: float, launch_angle_deg: float) -> float:
    """Rough backspin estimate from launch conditions.

    Real spin needs either radar or a marked ball at very high frame rate.
    OpenFlight's own spin detection is experimental and it does not feed spin
    into carry for exactly this reason. This is a stand-in so the drag/lift
    model has something plausible to work with, NOT a measurement.

    Built from the well-known pattern that spin rises steeply as loft rises:
    a driver at ~11 deg launches near 2500 rpm, a 7-iron at ~19 deg near
    7000 rpm, a wedge at ~30 deg near 10000 rpm.
    """
    a = max(0.0, launch_angle_deg)
    # Piecewise-linear through the three anchor points above.
    if a <= 11.0:
        rpm = 2000.0 + (a / 11.0) * 500.0
    elif a <= 19.0:
        rpm = 2500.0 + ((a - 11.0) / 8.0) * 4500.0
    elif a <= 30.0:
        rpm = 7000.0 + ((a - 19.0) / 11.0) * 3000.0
    else:
        rpm = 10000.0
    # Slower swings spin a little less at the same launch.
    speed_scale = min(1.15, max(0.75, ball_speed_ms / 60.0))
    return rpm * speed_scale


def _drag_coefficient(reynolds_proxy: float, spin_ratio: float) -> float:
    """Dimpled-ball drag. Cd sits near 0.22 in the post-critical regime and
    climbs with spin ratio."""
    return 0.21 + 0.45 * min(spin_ratio, 0.35)


def _lift_coefficient(spin_ratio: float) -> float:
    """Magnus lift. Roughly linear in spin ratio, saturating high."""
    return min(0.33, 1.65 * spin_ratio)


def simulate(launch: Launch, dt: float = 0.002, max_t: float = 15.0) -> Flight:
    """Integrate the flight with drag and Magnus lift (RK-free, small dt).

    2D in the vertical plane. Azimuth only rotates the result, it does not
    change carry, so it is ignored here.
    """
    theta = math.radians(launch.launch_angle_deg)
    vx = launch.ball_speed_ms * math.cos(theta)
    vy = launch.ball_speed_ms * math.sin(theta)
    x = 0.0
    y = 0.0

    omega = launch.backspin_rpm * 2.0 * math.pi / 60.0  # rad/s
    apex = 0.0
    t = 0.0
    prev = (x, y, vx, vy)

    while t < max_t:
        v = math.hypot(vx, vy)
        if v < 1e-6:
            break

        spin_ratio = (omega * BALL_RADIUS) / v
        cd = _drag_coefficient(v, spin_ratio)
        cl = _lift_coefficient(spin_ratio)

        q = 0.5 * RHO * BALL_AREA * v * v
        drag = q * cd
        lift = q * cl

        # Drag opposes velocity; lift is perpendicular, rotated +90 deg for backspin.
        ax = (-drag * (vx / v) - lift * (vy / v)) / BALL_MASS
        ay = (-drag * (vy / v) + lift * (vx / v)) / BALL_MASS - G

        prev = (x, y, vx, vy)
        vx += ax * dt
        vy += ay * dt
        x += vx * dt
        y += vy * dt
        t += dt

        if y > apex:
            apex = y
        if y <= 0.0 and t > dt * 2:
            break

    # Linear interpolation back to ground crossing.
    px, py, pvx, pvy = prev
    if py != y and y < 0.0:
        frac = py / (py - y)
        x = px + (x - px) * frac
        t = t - dt * (1.0 - frac)
        vx = pvx + (vx - pvx) * frac
        vy = pvy + (vy - pvy) * frac

    descent = math.degrees(math.atan2(-vy, max(vx, 1e-6)))
    return Flight(carry_m=x, apex_m=apex, hang_time_s=t, descent_angle_deg=descent)


def smash_factor(ball_speed_ms: float, club_speed_ms: float) -> float:
    if club_speed_ms <= 0:
        return float("nan")
    return ball_speed_ms / club_speed_ms


def ms_to_mph(v: float) -> float:
    return v * MPH_PER_MS


def mph_to_ms(v: float) -> float:
    return v / MPH_PER_MS
