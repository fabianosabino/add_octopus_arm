-- SimpleClaw v3.0 - System Database Initialization
-- ==================================================
-- Este script roda no banco 'simpleclaw' (system).

CREATE SCHEMA IF NOT EXISTS system;
CREATE SCHEMA IF NOT EXISTS agent;

CREATE EXTENSION IF NOT EXISTS vector;

GRANT ALL ON SCHEMA system TO simpleclaw;
GRANT ALL ON SCHEMA agent TO simpleclaw;
