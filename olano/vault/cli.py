"""Vault CLI commands (mounted as `olano vault` subgroup)."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

console = Console()


def _open_vault(passphrase: str | None = None):
    from olano.vault.store import VaultStore

    return VaultStore.open(passphrase=passphrase)


@click.group()
@click.option(
    "--passphrase",
    "-p",
    default=None,
    envvar=["OLANO_VAULT_KEY", "LIONCLAW_VAULT_KEY"],
    help="Master passphrase",
)
@click.pass_context
def vault_group(ctx: click.Context, passphrase: str | None) -> None:
    """Manage encrypted credentials."""
    ctx.ensure_object(dict)
    ctx.obj["passphrase"] = passphrase


@vault_group.command("init")
@click.pass_context
def vault_init(ctx: click.Context) -> None:
    """Initialize the vault (creates the encrypted store)."""
    vault = _open_vault(ctx.obj.get("passphrase"))
    console.print("[green]✓ Vault initialized.[/green]")
    console.print(f"  Location: {vault._db_path}")
    console.print("  Set OLANO_VAULT_KEY or install 'keyring' for automatic unlock.")


@vault_group.command("set")
@click.argument("name")
@click.option("--value", "-v", default=None, help="Credential value (prompted if omitted)")
@click.option("--description", "-d", default="", help="Optional description")
@click.option("--category", "-c", default="general", help="Category for grouping")
@click.pass_context
def vault_set(
    ctx: click.Context, name: str, value: str | None, description: str, category: str
) -> None:
    """Store or update a credential."""
    if not value:
        value = click.prompt(f"Value for '{name}'", hide_input=True, confirmation_prompt=True)
    vault = _open_vault(ctx.obj.get("passphrase"))
    vault.set(name, value, description=description, category=category)
    console.print(f"[green]✓ Stored '{name}'[/green]")


@vault_group.command("get")
@click.argument("name")
@click.option("--show", is_flag=True, help="Print the value to stdout (careful!)")
@click.pass_context
def vault_get(ctx: click.Context, name: str, show: bool) -> None:
    """Retrieve a credential (prints masked by default)."""
    from olano.vault.store import VaultError

    vault = _open_vault(ctx.obj.get("passphrase"))
    try:
        value = vault.get(name)
        if show:
            console.print(value)
        else:
            masked = value[:4] + "*" * (len(value) - 4) if len(value) > 4 else "****"
            console.print(f"'{name}': {masked} (use --show to reveal)")
    except VaultError as e:
        console.print(f"[red]Error:[/red] {e}")


@vault_group.command("list")
@click.option("--category", "-c", default=None, help="Filter by category")
@click.pass_context
def vault_list(ctx: click.Context, category: str | None) -> None:
    """List stored credentials (names only, no values)."""
    vault = _open_vault(ctx.obj.get("passphrase"))
    creds = vault.list_names(category=category)

    if not creds:
        console.print("[yellow]No credentials stored.[/yellow]")
        return

    table = Table(title="Stored Credentials", show_header=True)
    table.add_column("Name", style="bold")
    table.add_column("Category")
    table.add_column("Description")
    table.add_column("Updated")
    table.add_column("Last Used")

    for c in creds:
        table.add_row(
            c["name"],
            c["category"],
            c["description"] or "-",
            (c["updated_at"] or "")[:10],
            (c["last_used"] or "never")[:10],
        )
    console.print(table)


@vault_group.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to delete this credential?")
@click.pass_context
def vault_delete(ctx: click.Context, name: str) -> None:
    """Delete a credential from the vault."""
    vault = _open_vault(ctx.obj.get("passphrase"))
    if vault.delete(name):
        console.print(f"[green]✓ Deleted '{name}'[/green]")
    else:
        console.print(f"[yellow]Credential '{name}' not found.[/yellow]")


@vault_group.command("audit")
@click.pass_context
def vault_audit(ctx: click.Context) -> None:
    """Show credentials due for rotation."""
    vault = _open_vault(ctx.obj.get("passphrase"))
    due = vault.needs_rotation()
    if not due:
        console.print("[green]✓ All credentials are up to date.[/green]")
    else:
        console.print(f"[yellow]⚠ {len(due)} credential(s) due for rotation:[/yellow]")
        for name in due:
            console.print(f"  - {name}")
