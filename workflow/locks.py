"""파일 잠금: 같은 params 파일·같은 실행에 대한 동시 승인을 막는다 (O_EXCL 잠금 파일, 운영체제 무관)."""

import os
from contextlib import contextmanager
from pathlib import Path


class LockBusy(ValueError):
    pass


@contextmanager
def file_lock(path: str | Path, what: str):
    path = Path(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise LockBusy(f"{what}이(가) 이미 진행 중 (잠금 파일 {path}). "
                       "진행 중인 프로세스가 없으면 이 파일을 지우고 다시 실행하라") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
