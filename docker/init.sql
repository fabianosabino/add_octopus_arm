-- SimpleClaw v2.0 - Database Initialization
-- ============================================

CREATE SCHEMA IF NOT EXISTS system;
CREATE SCHEMA IF NOT EXISTS agent;

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Grant permissions
GRANT ALL ON SCHEMA system TO simpleclaw;
GRANT ALL ON SCHEMA agent TO simpleclaw;
