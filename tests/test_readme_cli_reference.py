from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from xvt.cli import build_parser


class ReadmeCliReferenceTests(unittest.TestCase):
    def test_reference_covers_every_command_and_argument(self) -> None:
        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        reference = readme.split("<!-- cli-reference:start -->", 1)[1].split(
            "<!-- cli-reference:end -->", 1
        )[0]
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )

        self.assertIn("`--version`", reference)
        for command, command_parser in subparsers.choices.items():
            heading = f"<summary><code>xvt {command}</code>"
            self.assertIn(heading, reference)
            command_reference = reference.split(heading, 1)[1].split("</details>", 1)[0]
            for action in command_parser._actions:
                if action.dest == "help":
                    continue
                argument = action.option_strings[-1] if action.option_strings else action.dest
                self.assertIn(
                    argument,
                    command_reference,
                    f"README reference for xvt {command} is missing {argument}",
                )


if __name__ == "__main__":
    unittest.main()
