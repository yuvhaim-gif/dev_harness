"""The mutate -> enforce -> autorepair/reconcile state machine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from runner_containment import _containment_abort
from runner_core import BUDGET_ABORT_EXIT, CONTAINMENT_ABORT_EXIT, RunContext, log
from runner_reconcile import reconcile
from runner_recovery import _budget_abort, _guard_abort, _timeout_abort, autorepair
from runner_states import enforce, mutate

_POST_MUTATE_ABORTS: tuple[tuple[Callable[[RunContext], bool], int], ...] = (
    (_budget_abort, BUDGET_ABORT_EXIT),
    (_timeout_abort, BUDGET_ABORT_EXIT),
    (_guard_abort, CONTAINMENT_ABORT_EXIT),
    (_containment_abort, CONTAINMENT_ABORT_EXIT),
)


# After autorepair nothing new is committed yet, so containment is not re-checked
# here -- it runs after the next iteration's enforce instead.
_POST_REPAIR_ABORTS: tuple[tuple[Callable[[RunContext], bool], int], ...] = (
    (_budget_abort, BUDGET_ABORT_EXIT),
    (_timeout_abort, BUDGET_ABORT_EXIT),
    (_guard_abort, CONTAINMENT_ABORT_EXIT),
)


@dataclass
class DriveModel:
    """Side-effecting steps and abort checks of the drive loop, injected so the
    transition logic can be unit-tested with fakes instead of subprocesses."""

    mutate: Callable[[RunContext], None]
    enforce: Callable[[RunContext], tuple[str, str]]
    autorepair: Callable[[RunContext], bool]
    reconcile: Callable[[RunContext], int]
    containment: Callable[[RunContext], bool]
    post_mutate_aborts: tuple[tuple[Callable[[RunContext], bool], int], ...]
    post_repair_aborts: tuple[tuple[Callable[[RunContext], bool], int], ...]


def _default_drive_model() -> DriveModel:
    return DriveModel(
        mutate=mutate,
        enforce=enforce,
        autorepair=autorepair,
        reconcile=reconcile,
        containment=_containment_abort,
        post_mutate_aborts=_POST_MUTATE_ABORTS,
        post_repair_aborts=_POST_REPAIR_ABORTS,
    )


def _first_abort(
    ctx: RunContext, checks: tuple[tuple[Callable[[RunContext], bool], int], ...]
) -> int | None:
    for check, code in checks:
        if check(ctx):
            return code
    return None


def run_drive(ctx: RunContext, model: DriveModel) -> int:
    """Run the mutate -> enforce -> autorepair/reconcile machine to a terminal code.

    Transitions per iteration:
      mutate -> (post-mutate aborts) -> enforce
        "dry-run"             -> reconcile (terminal)
        "mechanical"          -> enforce once more, then fall through
        "passed"              -> containment check, else reconcile (terminal)
        "semantic"/mechanical -> autorepair; cap exit 1, else (post-repair aborts), loop
    """
    while True:
        model.mutate(ctx)
        code = _first_abort(ctx, model.post_mutate_aborts)
        if code is not None:
            return code

        status, log_text = model.enforce(ctx)
        if status == "dry-run":
            return model.reconcile(ctx)
        if status == "mechanical":
            log("mechanical hook fix detected; re-staging and retrying once.")
            status, log_text = model.enforce(ctx)
        if status == "passed":
            if model.containment(ctx):
                return CONTAINMENT_ABORT_EXIT
            return model.reconcile(ctx)

        # semantic (or still mechanical after the single retry) -> autorepair
        ctx.last_hook_log = log_text
        ctx.last_status = status
        if not model.autorepair(ctx):
            return 1
        code = _first_abort(ctx, model.post_repair_aborts)
        if code is not None:
            return code


def _drive(ctx: RunContext) -> int:
    return run_drive(ctx, _default_drive_model())
