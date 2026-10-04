import sys

from .cli import main

# 출력이 한국어라서, 콘솔·파이프 인코딩이 UTF-8이 아닌 환경(예: Windows cp1252)에서도 깨지지 않게 한다
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.exit(main())
