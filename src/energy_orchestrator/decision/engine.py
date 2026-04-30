"""Decision engine: applies rules in priority order, then layers manual override."""

from __future__ import annotations

from collections.abc import Sequence

from energy_orchestrator.config.models import DecisionConfig
from energy_orchestrator.decision.context import DecisionRecord, TickContext
from energy_orchestrator.decision.rules import DEFAULT_RULES, Rule, RuleOutcome


class DecisionEngine:
    def __init__(
        self,
        config: DecisionConfig,
        rules: Sequence[Rule] = DEFAULT_RULES,
    ) -> None:
        self.config = config
        self.rules: tuple[Rule, ...] = tuple(rules)

    def decide(self, ctx: TickContext) -> DecisionRecord:
        outcome: RuleOutcome | None = None
        rule_fired: str | None = None
        for rule in self.rules:
            outcome = rule.evaluate(ctx, self.config)
            if outcome is not None:
                rule_fired = rule.name
                break
        if outcome is None or rule_fired is None:
            raise RuntimeError(
                "no rule produced an outcome — ensure a fallback rule is in the chain"
            )

        auto_state = outcome.state

        if ctx.override is not None and ctx.override.is_active(ctx.timestamp):
            final_state = ctx.override.forced_state
            manual_override = True
            override_mode = ctx.override.mode
        else:
            final_state = auto_state
            manual_override = False
            override_mode = None

        state_changed = ctx.previous_state != final_state

        return DecisionRecord(
            timestamp=ctx.timestamp,
            state=final_state,
            rule_fired=rule_fired,
            reason=outcome.reason,
            state_changed=state_changed,
            manual_override=manual_override,
            override_mode=override_mode,
            forecast_end_soc_pct=outcome.forecast_end_soc_pct,
        )
