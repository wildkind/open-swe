import logging
import os
import time

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig, DaytonaError
from daytona.common.errors import DaytonaNotFoundError, DaytonaRateLimitError
from daytona_api_client.models import SandboxState
from langchain_daytona import DaytonaSandbox

logger = logging.getLogger(__name__)

# TODO: Update this to include your specific sandbox configuration
DAYTONA_SANDBOX_PARAMS = CreateSandboxFromSnapshotParams(snapshot="daytona-medium")

_CREATE_MAX_ATTEMPTS = 3
_CREATE_BACKOFF_BASE_SECONDS = 2.0

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


def _is_retryable_create_error(err: DaytonaError) -> bool:
    """Return True for transient errors worth retrying on sandbox creation.

    `status_code is None` means the request never got an HTTP response — i.e. a
    network/transport failure (read timeout, connection reset). 5xx and 429 are
    server-side hiccups that usually resolve on retry. 4xx (other than 429) and
    not-found errors indicate a real problem and should not be retried.
    """
    if isinstance(err, DaytonaNotFoundError):
        return False
    if isinstance(err, DaytonaRateLimitError):
        return True
    if err.status_code is None:
        return True
    return err.status_code >= 500


def _create_sandbox_with_retry(daytona: Daytona):
    """Call daytona.create with retries on transient errors."""
    for attempt in range(1, _CREATE_MAX_ATTEMPTS + 1):
        try:
            return daytona.create(params=DAYTONA_SANDBOX_PARAMS)
        except DaytonaError as err:
            if attempt == _CREATE_MAX_ATTEMPTS or not _is_retryable_create_error(err):
                _log_daytona_error("create sandbox", err)
                raise
            backoff = _CREATE_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Transient Daytona error on create attempt %d/%d (status=%s): %s. "
                "Retrying in %.1fs",
                attempt,
                _CREATE_MAX_ATTEMPTS,
                err.status_code,
                err,
                backoff,
            )
            time.sleep(backoff)


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
        sandbox = _create_sandbox_with_retry(daytona)

    return DaytonaSandbox(sandbox=sandbox)
