"""Bounded cleanup for directly owned workers and their isolated process groups."""

import os
import signal
import subprocess
import sys

from bijux_ontology.diagnostics.errors import InputError, ResourceError


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
    Windows terminates the supervisor, closing its descendant-owning Job.
    Persistent cleanup errors cannot certify success. With an existing failure,
    attach cleanup evidence to it;
    the caller retains responsibility for propagating that failed outcome.
    """
    failures: list[str] = []
    group_failure: str | None = None
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
            if os.name == "posix":
                group_failure = str(error)
            else:
                failures.append(str(error))
        else:
            if force:
                # Final group escalation still runs after leader exit. Its
                # success supersedes an earlier transient signalling failure;
                # the direct-child wait below must also establish actual reap.
                group_failure = None
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
    if group_failure is not None:
        failures.append(group_failure)
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


def spawn_owned_process(
    command: list[str] | tuple[str, ...],
    *,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.Popen[bytes]:
    """Launch one supported owned command with platform descendant guarantees.

    POSIX uses an isolated process group. Windows uses a supervisor owning an
    atomic kill-on-close Job assignment and the exact caller process lifetime.
    The supervisor shares only the requested binary standard streams with the
    actual command. Unsupported ownership guarantees fail before semantic work.
    """
    if not command or any(not isinstance(value, str) for value in command):
        raise InputError("Owned processes require a nonempty argument list")
    if os.name == "nt":
        from bijux_ontology.execution.processes.windows import parent_identity

        parent, created = parent_identity()
        command = [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "bijux_ontology.execution.processes.windows",
            str(parent),
            str(created),
            "--",
            *command,
        ]
    elif os.name != "posix":
        raise ResourceError("Owned descendant cleanup is unavailable on this platform")
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=os.name == "posix",
    )
