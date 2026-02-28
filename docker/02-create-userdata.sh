#!/bin/bash
# SimpleClaw v3.0 - Create userdata database
# ============================================
# Runs after init.sql. Creates the isolated user database.

set -e

echo "Creating simpleclaw_data database..."

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Create userdata database if it doesn't exist
    SELECT 'CREATE DATABASE simpleclaw_data OWNER simpleclaw'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'simpleclaw_data')\gexec
EOSQL

# Configure the new database
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "simpleclaw_data" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    GRANT ALL ON SCHEMA public TO simpleclaw;
EOSQL

echo "simpleclaw_data database ready."
