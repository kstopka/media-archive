"""Use the state connection and transaction supplied by the application."""

import sqlite3

from alembic import context


def _authorize_migration(
    action: int, name: str | None, argument: str | None,
    database: str | None, source: str | None,
) -> int:
    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                  sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and name not in (
        "database_list", "table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list",
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def run_migrations() -> None:
    connection = context.config.attributes["connection"]
    context.configure(connection=connection, version_table_schema="main", transactional_ddl=True)
    driver = connection.connection.driver_connection
    driver.set_authorizer(_authorize_migration)
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        driver.set_authorizer(None)


run_migrations()
