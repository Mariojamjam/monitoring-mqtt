"""Storage behind the API: PostgreSQL schema, migrations, writes and queries.

Only the API (and the one-off ``migrate`` service) imports this package. Consumers hand
their events to the API over HTTP and never open a database connection.
"""
