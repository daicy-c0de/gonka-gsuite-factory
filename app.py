#!/usr/bin/env python3
"""Entry point: CLI mode (--email/--password/--tempmail/--count) atau Telegram bot (--tg)."""
import sys


def main():
    if "--tg" in sys.argv[1:]:
        sys.argv.remove("--tg")
        from gonkafarm.telegram import run
        run()
        return
    from gonkafarm.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
