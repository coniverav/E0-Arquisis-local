FROM postgres:16-alpine

# pg_isready viene incluido en la imagen oficial de PostgreSQL.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
  CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1