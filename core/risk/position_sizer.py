# -*- coding: utf-8 -*-
"""
KHAOS Position Sizer v3.0 – Wall Street Institutional Grade
===========================================================
Core responsibility: compute safe, executable order quantities based on
risk budget, volatility, account adaptation, and resonance.
Fully validated for accounts as small as $2,000 USD.

All calculations are deterministic, auditable, and guarded against edge cases.
Supports both traditional keyword arguments and a `PositionRequest` dataclass.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class PositionSizerError(Exception):
    """Base exception for position sizing errors."""

class InvalidParameterError(PositionSizerError):
    """One or more input parameters are invalid."""

class QuantityBelowMinimumError(PositionSizerError):
    """The computed quantity is below the minimum tradable amount."""

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request dataclass (allows both kwarg and structured calls)
# ---------------------------------------------------------------------------
@dataclass
class PositionRequest:
    equity: float
    atr: float
    price: float
    min_qty: float
    step_size: float
    direction: str = "LONG"
    resonance_multiplier: float = 1.0
    adaptation_scale: float = 1.0
    stop_distance_atr: Optional[float] = None
    current_notional: float = 0.0
    estimated_fee_pct: float = 0.0          # e.g., 0.001 = 10 bps
    min_notional_override: Optional[float] = None
    request_id: str = ""


# ---------------------------------------------------------------------------
# Position Sizer
# ---------------------------------------------------------------------------
class PositionSizer:
    """
    Dynamic position size calculator.

    Typical usage:
        sizer = PositionSizer(base_risk=0.01)
        qty = sizer.calculate(equity=2000, atr=100, price=60000,
                              min_qty=0.001, step_size=0.001)
    """

    def __init__(
        self,
        base_risk_per_trade: float = 0.01,
        max_risk_per_trade: float = 0.03,
        min_risk_per_trade: float = 0.002,
        max_leverage: float = 3.0,
        auto_risk_adjust: bool = True,
        default_stop_distance_atr: float = 1.5,
        min_notional_usd: float = 10.0,
        max_position_notional: float = 1_000_000.0,
        risk_adjust_step: float = 0.001,
        max_adjust_iterations: int = 30,
    ):
        # ── parameter validation ────────────────────────────────
        if not (0 < min_risk_per_trade <= base_risk_per_trade <= max_risk_per_trade <= 0.1):
            raise InvalidParameterError(
                "Risk fractions must satisfy 0 < min ≤ base ≤ max ≤ 0.1"
            )
        if max_leverage <= 0:
            raise InvalidParameterError("max_leverage must be > 0")
        if default_stop_distance_atr <= 0:
            raise InvalidParameterError("default_stop_distance_atr must be > 0")
        if min_notional_usd < 0:
            raise InvalidParameterError("min_notional_usd cannot be negative")
        if risk_adjust_step <= 0 or risk_adjust_step > 0.01:
            raise InvalidParameterError("risk_adjust_step should be in (0, 0.01]")

        self.base_risk_per_trade = base_risk_per_trade
        self.max_risk_per_trade = max_risk_per_trade
        self.min_risk_per_trade = min_risk_per_trade
        self.max_leverage = max_leverage
        self.auto_risk_adjust = auto_risk_adjust
        self.default_stop_distance_atr = default_stop_distance_atr
        self.min_notional_usd = min_notional_usd
        self.max_position_notional = max_position_notional
        self.risk_adjust_step = risk_adjust_step
        self.max_adjust_iterations = max_adjust_iterations

    # ------------------------------------------------------------------
    # Primary public method
    # ------------------------------------------------------------------
    def calculate(
        self,
        equity: float,
        atr: float,
        price: float,
        min_qty: float,
        step_size: float,
        direction: str = "LONG",
        resonance_multiplier: float = 1.0,
        adaptation_scale: float = 1.0,
        stop_distance_atr: Optional[float] = None,
        current_notional: float = 0.0,
        estimated_fee_pct: float = 0.0,
        request_id: str = "",
    ) -> Optional[float]:
        """
        Calculate the executable quantity for a single order.

        Returns ``None`` if the resulting quantity is below ``min_qty``
        and cannot be rescued by risk adjustment.
        """
        req = PositionRequest(
            equity=equity,
            atr=atr,
            price=price,
            min_qty=min_qty,
            step_size=step_size,
            direction=direction,
            resonance_multiplier=resonance_multiplier,
            adaptation_scale=adaptation_scale,
            stop_distance_atr=stop_distance_atr,
            current_notional=current_notional,
            estimated_fee_pct=estimated_fee_pct,
            request_id=request_id,
        )
        return self._calculate(req)

    def calculate_from_request(self, req: PositionRequest) -> Optional[float]:
        """Alternative entry point using a `PositionRequest` dataclass."""
        return self._calculate(req)

    # ------------------------------------------------------------------
    # Internal calculation
    # ------------------------------------------------------------------
    def _calculate(self, req: PositionRequest) -> Optional[float]:
        # 1. guard
        if not self._guard_inputs(req):
            return None

        stop_atr = req.stop_distance_atr or self.default_stop_distance_atr
        stop_atr = max(0.3, stop_atr)  # prevent ultra-tight stops

        # clamp scaling factors
        resonance = max(0.1, min(2.0, req.resonance_multiplier))
        adaptation = max(0.1, min(1.5, req.adaptation_scale))

        # 2. core variables
        risk_pct = self.base_risk_per_trade
        risk_amount = req.equity * risk_pct * (1.0 - req.estimated_fee_pct)
        stop_distance = stop_atr * req.atr
        if stop_distance <= 0:
            logger.error("Zero stop distance")
            return None

        raw_qty = max(0.0, risk_amount / stop_distance) * adaptation * resonance

        # 3. leverage cap (single order notional)
        max_notional = req.equity * self.max_leverage
        max_notional = min(max_notional, self.max_position_notional)
        max_qty = max_notional / req.price if req.price > 0 else float("inf")
        raw_qty = min(raw_qty, max_qty)

        # 4. min notional check
        effective_min_notional = req.min_notional_override or self.min_notional_usd
        if raw_qty * req.price < effective_min_notional:
            logger.debug("Notional below minimum %.2f USD", effective_min_notional)
            return None

        # 5. rounding
        qty = self._round_down(raw_qty, req.step_size)

        # 6. if below min qty, attempt risk upsize
        if qty < req.min_qty - 1e-12:
            if not self.auto_risk_adjust:
                logger.info("Qty below min & auto adjust disabled")
                return None
            res = self._try_upsize_risk(req, stop_atr, resonance, adaptation)
            if res is None:
                return None
            qty, risk_pct = res
        else:
            # record actual risk used (base risk)
            pass

        # 7. final lever check
        if qty * req.price > max_notional:
            qty = self._round_down(max_notional / req.price, req.step_size)
            if qty < req.min_qty:
                return None

        logger.info(
            "Position sized: qty=%.6f, risk=%.2f%%, notional=%.2f",
            qty, risk_pct * 100, qty * req.price,
        )
        return qty

    # ------------------------------------------------------------------
    # Risk upsize helper
    # ------------------------------------------------------------------
    def _try_upsize_risk(
        self, req: PositionRequest, stop_atr: float, resonance: float, adaptation: float
    ) -> Optional[Tuple[float, float]]:
        """Try increasing risk fraction up to max_risk to meet min_qty."""
        risk = self.base_risk_per_trade
        for _ in range(self.max_adjust_iterations):
            if risk > self.max_risk_per_trade:
                break
            amount = req.equity * risk * (1.0 - req.estimated_fee_pct)
            raw_qty = max(0.0, amount / (stop_atr * req.atr)) * adaptation * resonance
            max_notional = req.equity * self.max_leverage
            max_notional = min(max_notional, self.max_position_notional)
            raw_qty = min(raw_qty, max_notional / req.price)
            qty = self._round_down(raw_qty, req.step_size)

            effective_min_notional = req.min_notional_override or self.min_notional_usd
            if qty >= req.min_qty - 1e-12 and qty * req.price >= effective_min_notional:
                logger.debug("Risk upsize succeeded at %.2f%%", risk * 100)
                return qty, risk
            risk += self.risk_adjust_step

        return None

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    @staticmethod
    def estimate_qty(equity: float, price: float, risk_pct: float,
                     stop_distance: float) -> float:
        """Quick, non-validated estimate."""
        if equity <= 0 or price <= 0 or stop_distance <= 0:
            return 0.0
        return (equity * risk_pct) / stop_distance

    def validate_qty(self, qty: float, min_qty: float, step_size: float) -> Optional[float]:
        """Validate and round a quantity to exchange requirements."""
        rounded = self._round_down(qty, step_size)
        return rounded if rounded >= min_qty - 1e-12 else None

    @staticmethod
    def _round_down(value: float, step: float) -> float:
        if step <= 0:
            return max(0.0, value)
        return max(0.0, math.floor(value / step) * step)

    @staticmethod
    def _guard_inputs(req: PositionRequest) -> bool:
        fields = {
            "equity": req.equity, "atr": req.atr, "price": req.price,
            "min_qty": req.min_qty, "step_size": req.step_size,
        }
        for name, val in fields.items():
            if not math.isfinite(val) or val <= 0:
                logger.error("Invalid input %s = %s", name, val)
                return False
        if req.min_qty < req.step_size:
            logger.warning("min_qty < step_size may cause issues")
        if req.atr > req.price * 0.5:
            logger.warning("Extreme volatility: ATR > 50%% of price")
        if req.equity < 500:
            logger.info("Small account mode: equity = %.2f", req.equity)
        return True

    def __repr__(self) -> str:
        return (f"PositionSizer(risk={self.base_risk_per_trade:.2%}-{self.max_risk_per_trade:.2%}, "
                f"leverage={self.max_leverage}x, adjust={self.auto_risk_adjust})")
