import argparse
import io
from typing import TextIO

import pytest

from ownvitals import cli
from ownvitals.__main__ import main
from ownvitals.config import Settings


def parser_with_subcommands() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ownvitals")
    cli.add_subcommands(parser)
    return parser


def test_without_a_subcommand_the_server_starts() -> None:
    assert parser_with_subcommands().parse_args([]).command is None


def test_an_unknown_subcommand_is_refused_by_the_parser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parser_with_subcommands().parse_args(["nope"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_run_command_refuses_a_command_nobody_registered() -> None:
    err = io.StringIO()
    code = cli.run_command(argparse.Namespace(command="nope"), Settings(), io.StringIO(), err)
    assert code == 2
    assert "unknown command 'nope'" in err.getvalue()


def test_a_registered_command_runs_on_its_own_loop_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hello(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
        print(f"hello {args.name}", file=out)
        print("a diagnostic", file=err)
        return 3

    monkeypatch.setitem(cli.COMMANDS, "hello", hello)
    out, err = io.StringIO(), io.StringIO()
    code = cli.run_command(argparse.Namespace(command="hello", name="you"), Settings(), out, err)
    assert (code, out.getvalue(), err.getvalue()) == (3, "hello you\n", "a diagnostic\n")


@pytest.mark.usefixtures("restore_logging")
def test_main_runs_a_registered_subcommand_instead_of_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ran(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
        print("ran", file=out)
        return 0

    def with_ran(parser: argparse.ArgumentParser) -> None:
        parser.add_subparsers(dest="command").add_parser("ran")

    def no_server(*_: object) -> None:
        raise AssertionError("a subcommand must not start the server")

    monkeypatch.setattr("ownvitals.__main__.add_subcommands", with_ran)
    monkeypatch.setattr("ownvitals.__main__.Server.run", no_server)
    monkeypatch.setitem(cli.COMMANDS, "ran", ran)
    assert main(["ran"]) == 0
