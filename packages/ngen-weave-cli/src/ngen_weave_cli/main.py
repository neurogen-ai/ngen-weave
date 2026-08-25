"""ngen-weave CLI entry point."""

from importlib.metadata import version

import typer


def _print_version(value: bool) -> None:
    if value:
        typer.echo(version("ngen-weave"))
        raise typer.Exit()


app = typer.Typer(
    name="ngen-weave",
    help="Durable human-in-the-loop AI workflows on LangGraph.",
    no_args_is_help=True,
)


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Print the ngen-weave version and exit.",
    ),
) -> None:
    """Durable human-in-the-loop AI workflows on LangGraph."""
