from __future__ import annotations

from rethink.config import Budget
from rethink.schemas import Maturity


def maturity_after_script(script: str) -> Maturity:
    if script == "setup.sh":
        return Maturity.INSTALLABILITY
    if script == "doctor.sh":
        return Maturity.INSTALLABILITY
    if script == "verify.sh":
        return Maturity.TESTABILITY
    return Maturity.NONE


def timeout_for_script(script: str, budget: Budget) -> int:
    if script == "doctor.sh":
        return budget.doctor_timeout_sec
    if script == "setup.sh":
        return budget.setup_timeout_sec
    if script == "verify.sh":
        return budget.strongest_verify_timeout_sec
    return 300
