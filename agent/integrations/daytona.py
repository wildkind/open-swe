import logging
import os

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig, DaytonaError
from daytona_api_client.models import SandboxState
from langchain_daytona import DaytonaSandbox

logger = logging.getLogger(__name__)

# TODO: Update this to include your specific sandbox configuration
DAYTONA_SANDBOX_PARAMS = CreateSandboxFromSnapshotParams(snapshot="daytona-medium")

_TRANSITIONAL_STATES = (
    SandboxState.STARTING,
    SandboxState.RESTORING,
    SandboxState.PULLING_SNAPSHOT,
)
_UNRECOVERABLE_STATES = (
    SandboxState.ERROR,
    SandboxState.BUILD_FAILED,
    SandboxState.DESTROYED,
    SandboxState.DESTROYING,
)


def _log_daytona_error(context: str, err: DaytonaError) -> None:
    logger.error(
        "Daytona error during %s: status=%s message=%s headers=%s",
        context,
        err.status_code,
        str(err),
        err.headers,
    )


def _resume_sandbox(sandbox, sandbox_id: str) -> None:
    """Bring an existing sandbox up to the STARTED state."""
    state = sandbox.state
    logger.info("Reconnected to sandbox %s in state %s", sandbox_id, state)

    if state == SandboxState.STARTED:
        return
    if state == SandboxState.STOPPED:
        sandbox.start()
        return
    if state == SandboxState.ARCHIVED:
        try:
            sandbox.start()
        except DaytonaError as err:
            _log_daytona_error(f"start archived sandbox {sandbox_id}", err)
            logger.info("Attempting recover() on archived sandbox %s", sandbox_id)
            sandbox.recover()
        return
    if state in _TRANSITIONAL_STATES:
        logger.info("Sandbox %s is %s, waiting for it to start", sandbox_id, state)
        sandbox.wait_for_sandbox_start(timeout=120)
        return
    if state in _UNRECOVERABLE_STATES:
        raise DaytonaError(
            f"Sandbox {sandbox_id} is in unrecoverable state: {state} "
            f"(error_reason={sandbox.error_reason})"
        )
    raise DaytonaError(f"Sandbox {sandbox_id} is in unhandled state: {state}")


def create_daytona_sandbox(sandbox_id: str | None = None):
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise ValueError("DAYTONA_API_KEY environment variable is required")

    daytona = Daytona(config=DaytonaConfig(api_key=api_key))

    if sandbox_id:
        try:
            sandbox = daytona.get(sandbox_id)
            _resume_sandbox(sandbox, sandbox_id)
        except DaytonaError as err:
            _log_daytona_error(f"resume sandbox {sandbox_id}", err)
            raise
    else:
        try:
            sandbox = daytona.create(params=DAYTONA_SANDBOX_PARAMS)
        except DaytonaError as err:
            _log_daytona_error("create sandbox", err)
            raise

    return DaytonaSandbox(sandbox=sandbox)
