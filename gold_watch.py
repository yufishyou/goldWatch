#!/usr/bin/env python3
"""金价观察工具入口。

用法：
    python3 gold_watch.py check --config config.toml --dry-run
    python3 gold_watch.py run --config config.toml
    python3 gold_watch.py test-push --config config.toml
"""

from goldwatch.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
