"""
Session logger — records every rep and produces a session report.

The key metric is self_initiated_pct: the fraction of grips the patient
completed WITHOUT needing EMS assistance. This is the recovery trend line.
Week 1: 5%. Week 8: 60%. That graph is the clinical value proposition.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from app.state import Confidence, GripType

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("app/logs/sessions")


@dataclass
class RepEvent:
    timestamp: float
    trigger_latency_ms: float        # time from TriggerEvent to EMS fire
    ems_duration_ms: int
    grip_type: GripType
    claude_confidence: Confidence
    target_object: str               # from brain response acknowledgement context
    patient_completed_reach: bool    # arm reached the object
    self_initiated: bool             # fingers closed WITHOUT EMS firing


@dataclass
class SessionReport:
    session_id: str
    date: str
    total_reps: int
    self_initiated_count: int
    self_initiated_pct: float        # primary recovery metric
    avg_trigger_latency_ms: float
    completion_rate: float
    duration_minutes: float
    reps: list[dict] = field(default_factory=list)


class SessionLogger:
    """
    Usage:
        logger = SessionLogger()
        logger.start_session()
        logger.log_rep(rep_event)
        report = logger.end_session()
    """

    def __init__(self, sessions_dir: Path = SESSIONS_DIR) -> None:
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._reps: list[RepEvent] = []
        self._session_start: Optional[float] = None
        self._session_id: Optional[str] = None

    def start_session(self) -> str:
        self._reps = []
        self._session_start = time.time()
        self._session_id = f"session_{int(self._session_start)}"
        logger.info("Session started: %s", self._session_id)
        return self._session_id

    def log_rep(self, rep: RepEvent) -> None:
        self._reps.append(rep)
        logger.info(
            "Rep %d logged | grip=%s confidence=%s self_initiated=%s latency=%.0fms",
            len(self._reps),
            rep.grip_type.value,
            rep.claude_confidence.value,
            rep.self_initiated,
            rep.trigger_latency_ms,
        )

    def end_session(self) -> Optional[SessionReport]:
        if not self._session_id or self._session_start is None:
            logger.warning("end_session called with no active session")
            return None

        duration_s = time.time() - self._session_start
        total = len(self._reps)

        if total == 0:
            logger.info("Session ended with no reps logged")
            return None

        self_initiated = sum(1 for r in self._reps if r.self_initiated)
        completed = sum(1 for r in self._reps if r.patient_completed_reach)
        avg_latency = sum(r.trigger_latency_ms for r in self._reps) / total

        import datetime
        report = SessionReport(
            session_id=self._session_id,
            date=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            total_reps=total,
            self_initiated_count=self_initiated,
            self_initiated_pct=round(self_initiated / total * 100, 1),
            avg_trigger_latency_ms=round(avg_latency, 1),
            completion_rate=round(completed / total * 100, 1),
            duration_minutes=round(duration_s / 60, 1),
            reps=[self._serialize_rep(r) for r in self._reps],
        )

        out_path = self.sessions_dir / f"{self._session_id}.json"
        with open(out_path, "w") as f:
            json.dump(asdict(report), f, indent=2, default=str)
        logger.info("Session report written: %s", out_path)

        self._print_summary(report)
        return report

    def _serialize_rep(self, rep: RepEvent) -> dict:
        d = asdict(rep)
        d["grip_type"] = rep.grip_type.value
        d["claude_confidence"] = rep.claude_confidence.value
        return d

    def _print_summary(self, report: SessionReport) -> None:
        print("\n" + "=" * 50)
        print(f"SESSION COMPLETE — {report.date}")
        print(f"  Total reps:        {report.total_reps}")
        print(f"  Self-initiated:    {report.self_initiated_count} ({report.self_initiated_pct}%)")
        print(f"  Completion rate:   {report.completion_rate}%")
        print(f"  Avg trigger lag:   {report.avg_trigger_latency_ms}ms")
        print(f"  Duration:          {report.duration_minutes} min")
        print(f"  Report:            app/logs/sessions/{report.session_id}.json")
        print("=" * 50 + "\n")
