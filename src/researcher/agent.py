from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from contracts.research import (
    AssetHypothesis,
    CatalystType,
    Confidence,
    Direction,
    EvidenceItem,
    Horizon,
    IngestedAlert,
    InstrumentType,
    LinkageType,
    ResearchReport,
    ResearchStatus,
    SourceTier,
)


class ResearcherAgent(Protocol):
    model_name: str | None
    prompt_version: str

    def analyze(self, alert: IngestedAlert) -> ResearchReport:
        ...


@dataclass(slots=True)
class FallbackResearcherAgent:
    prompt_version: str = "v1"
    model_name: str | None = None

    def analyze(self, alert: IngestedAlert) -> ResearchReport:
        market = alert.shift_alert.market
        category = str(market.get("category") or "").lower()
        catalyst_type = CatalystType.OTHER
        if "economic" in category:
            catalyst_type = CatalystType.ECONOMIC_DATA
        elif "politic" in category or "policy" in category:
            catalyst_type = CatalystType.POLICY

        evidence: list[EvidenceItem] = []
        url = str(market.get("polymarket_url") or "")
        if url:
            evidence.append(
                EvidenceItem(
                    url=url,
                    title=str(market.get("event_title") or market.get("question") or "Polymarket event"),
                    publisher="Polymarket",
                    published_at=None,
                    retrieved_at=datetime.now(tz=timezone.utc),
                    source_tier=SourceTier.SECONDARY,
                    relevance_score=0.25,
                    claim_tag="market_context",
                )
            )

        hypotheses: list[AssetHypothesis] = []
        raw_hypotheses = alert.shift_alert.metadata.get("asset_hypotheses", [])
        if isinstance(raw_hypotheses, list):
            for item in raw_hypotheses:
                if not isinstance(item, dict):
                    continue
                try:
                    hypotheses.append(
                        AssetHypothesis(
                            symbol=str(item["symbol"]).upper(),
                            instrument_type=InstrumentType(str(item.get("instrument_type", "equity"))),
                            direction=Direction(str(item.get("direction", "bullish"))),
                            horizon=Horizon(str(item.get("horizon", "intraday"))),
                            linkage_type=LinkageType(str(item.get("linkage_type", "macro_proxy"))),
                            confidence=Confidence(str(item.get("confidence", "medium"))),
                            tradable_universe=bool(item.get("tradable_universe", True)),
                            rationale_summary=str(item.get("rationale_summary", "Provided by upstream replay metadata.")),
                        )
                    )
                except Exception:
                    continue

        if hypotheses and evidence:
            status = ResearchStatus.PASS
            stage_summary = f"Research fallback passed with {len(hypotheses)} replay-provided asset hypothesis entries."
        else:
            status = ResearchStatus.DEFER
            stage_summary = "No external research provider configured, so the alert was persisted but deferred."

        return ResearchReport(
            status=status,
            catalyst_type=catalyst_type,
            catalyst_summary=str(market.get("question") or "Catalyst requires external research."),
            catalyst_started_at=None,
            freshness_sec=0,
            evidence=evidence,
            asset_hypotheses=hypotheses,
            unsupported_claims=[] if hypotheses else ["No asset hypotheses were available from research providers."],
            stage_summary=stage_summary,
        )
