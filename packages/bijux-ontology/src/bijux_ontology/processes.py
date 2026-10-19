"""Bounded cleanup for directly owned workers and their isolated process groups."""

import os
import signal
import subprocess

from bijux_ontology.errors import InputError, ResourceError


def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Retry a zombie-only group after reaping its exited owned leader."""
    for attempt in range(2):
        try:
            os.killpg(process.pid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError as error:
            if attempt:
                raise ResourceError(
                    "Permission denied signalling owned worker group"
                ) from error
            # Darwin excludes zombies from group signalling and may return EPERM.
            # Reap only our own leader, then retry to reach any live descendants.
            process.poll()
        except OSError as error:
            raise ResourceError("Cannot signal owned worker group") from error


def stop_owned_process(
    process: subprocess.Popen[bytes], *, failure: BaseException | None = None
) -> None:
    """Reap an owned child; POSIX callers must have created its isolated session.

    Always escalate the group after leader exit: descendants may still own pipes.
    Non-POSIX callers terminate the direct worker. Persistent cleanup errors cannot
    certify success. With an existing failure, attach cleanup evidence to it;
    the caller retains responsibility for propagating that failed outcome.
    """
    failures: list[str] = []
    for force in (False, True):
        try:
            if os.name == "posix":
                _signal_group(process, signal.SIGKILL if force else signal.SIGTERM)
            elif process.poll() is None:
                if not force:
                    process.terminate()
                else:
                    process.kill()
        except (ResourceError, OSError) as error:
            failures.append(str(error))
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            if force:
                failures.append("Owned worker did not exit after forced termination")
                # Group denial must not prevent direct cleanup of our own child.
                try:
                    process.kill()
                    process.wait(timeout=0.2)
                except (OSError, subprocess.TimeoutExpired) as error:
                    failures.append(f"Cannot reap owned worker: {error}")
    if failures:
        cleanup_error = ResourceError(
            "Worker cleanup failed: " + "; ".join(dict.fromkeys(failures))
        )
        if failure is not None:
            if isinstance(failure, InputError):
                failure.args = (f"{failure}; {cleanup_error}",)
            else:
                failure.add_note(str(cleanup_error))
            return
        raise cleanup_error
