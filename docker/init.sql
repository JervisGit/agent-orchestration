-- =============================================================
-- Agent Orchestration — local dev schema + seed data
-- Runs automatically on first postgres container start.
-- Re-apply manually with: psql -h localhost -U ao ao < docker/init.sql
-- =============================================================

-- ── Taxpayer mock data ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS taxpayers (
    tax_id              VARCHAR(20)   PRIMARY KEY,
    full_name           VARCHAR(200)  NOT NULL,
    email               VARCHAR(200),
    entity_type         VARCHAR(50)   DEFAULT 'Individual',
    filing_status       VARCHAR(50)   DEFAULT 'current',  -- current | arrears
    assessment_year     INT,
    assessed_amount     DECIMAL(15,2) DEFAULT 0,
    outstanding_balance DECIMAL(15,2) DEFAULT 0,
    payment_plan_active BOOLEAN       DEFAULT FALSE,
    penalty_count       INT           DEFAULT 0,
    notes               TEXT
);

INSERT INTO taxpayers VALUES
('SG-T001-2890','John Tan Wei Ming',    'john.tan@example.com', 'Individual','current',2024,  8500.00,    0.00, FALSE,0,'Timely filer; no prior waivers'),
('SG-T002-4471','Priya d/o Krishnan',   'priya.k@example.com',  'Individual','arrears',2024, 12300.00, 3200.00, TRUE, 1,'Active payment plan $400/month since Jan 2026'),
('SG-T003-6612','Lee Siew Buay',        'lsb@company.sg',       'Corporate', 'current',2024,245000.00,    0.00, FALSE,0,'Large corporate; clean record'),
('SG-T004-9934','Ahmad Bin Razali',     'ahmad.r@gmail.com',    'Individual','arrears',2023,  4200.00, 4200.00, FALSE,2,'Missed 2024 filing; 2 prior late penalties'),
('SG-T005-1122','Wong Mei Lin',         'wml@enterprise.sg',    'Individual','current',2024, 19800.00,    0.00, FALSE,0,'Filed extension last year; granted'),
('SG-T006-3301','Ravi Kumar s/o Nair',  'ravi.kumar@tech.sg',   'Individual','current',2024, 31500.00,    0.00, FALSE,0,'First-time extension request eligible'),
('SG-T007-8823','Tan Boon Kiat Pte Ltd','tbk@tbkpte.com',       'Corporate', 'current',2024,187000.00, 9800.00, FALSE,0,'Invoice dispute; partial payment made'),
('SG-T008-5594','Fatimah bte Hassan',   'fatimah.h@gmail.com',  'Individual','arrears',2024,  6700.00, 6700.00, FALSE,3,'Third penalty — waiver requires supervisor approval')
ON CONFLICT DO NOTHING;

-- ── AO Platform: workflow registry ────────────────────────────
CREATE TABLE IF NOT EXISTS ao_workflows (
    workflow_id  VARCHAR(100) PRIMARY KEY,
    app_id       VARCHAR(100),
    pattern      VARCHAR(50),
    description  TEXT,
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

-- ── AO Platform: workflow run history ─────────────────────────
CREATE TABLE IF NOT EXISTS ao_workflow_runs (
    run_id       VARCHAR(100) PRIMARY KEY,
    workflow_id  VARCHAR(100),
    status       VARCHAR(50)  DEFAULT 'queued',
    input_data   JSONB        DEFAULT '{}',
    output_data  JSONB,
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

-- ── AO Platform: guardrail policy registry ────────────────────
CREATE TABLE IF NOT EXISTS ao_policies (
    id          SERIAL       PRIMARY KEY,
    app_id      VARCHAR(100),
    name        VARCHAR(100),
    stage       VARCHAR(50),
    action      VARCHAR(50),
    params      JSONB        DEFAULT '{}',
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(app_id, name, stage)
);

-- ── AO Platform: HITL approval queue ──────────────────────────
CREATE TABLE IF NOT EXISTS ao_hitl_requests (
    request_id  VARCHAR(100) PRIMARY KEY,
    workflow_id VARCHAR(100),
    step_name   VARCHAR(100),
    status      VARCHAR(50)  DEFAULT 'pending',
    payload     JSONB        DEFAULT '{}',
    reviewer    VARCHAR(100),
    note        TEXT,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- ── AO Platform: tool registry ────────────────────────────────
CREATE TABLE IF NOT EXISTS ao_tools (
    id                 SERIAL          PRIMARY KEY,
    app_id             VARCHAR(100),
    name               VARCHAR(100),
    type               VARCHAR(50)     DEFAULT 'custom',
    description        TEXT,
    endpoint           TEXT,
    connection_secret  VARCHAR(200),
    params             JSONB           DEFAULT '{}',
    created_at         TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE(app_id, name)
);

-- ── AO Platform: registered apps ───────────────────────────────
CREATE TABLE IF NOT EXISTS ao_apps (
    app_id         VARCHAR(100)    PRIMARY KEY,
    display_name   VARCHAR(200),
    description    TEXT,
    pattern        VARCHAR(50),
    manifest_yaml  TEXT,
    created_at     TIMESTAMPTZ     DEFAULT NOW(),
    updated_at     TIMESTAMPTZ     DEFAULT NOW()
);

-- ── AO Platform: app agents (from manifest) ─────────────────────
CREATE TABLE IF NOT EXISTS ao_app_agents (
    id             SERIAL          PRIMARY KEY,
    app_id         VARCHAR(100)    REFERENCES ao_apps(app_id) ON DELETE CASCADE,
    agent_name     VARCHAR(100),
    model          VARCHAR(100),
    tool_names     TEXT[]          DEFAULT '{}',
    hitl_condition TEXT,
    created_at     TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE(app_id, agent_name)
);

-- ── Seed: default apps ──────────────────────────────────────────
INSERT INTO ao_apps (app_id, display_name, description, pattern) VALUES
('tax_email_assistant', 'Tax Email Assistant', 'Routes taxpayer emails to specialist agents with guardrail policy enforcement.', 'concurrent'),
('rag_search',          'RAG Search',          'Retrieval-augmented search over company documents with pgvector embeddings.',       'linear'),
('graph_compliance',    'Graph Compliance',    'Multi-agent compliance checks using Microsoft Graph API with user-delegated identity.', 'supervisor')
ON CONFLICT DO NOTHING;
-- ── RAG Search: document knowledge base (pgvector) ──────────────────
-- Requires the pgvector extension. The app calls LongTermMemory.initialize()
-- at startup which creates this table — no need to duplicate the CREATE here.
-- The extension itself must be pre-installed in the Postgres image.
CREATE EXTENSION IF NOT EXISTS vector;
-- ── Seed: default workflows for the AO Dashboard ──────────────
INSERT INTO ao_workflows (workflow_id, app_id, pattern, description) VALUES
('email-triage-v1',    'tax_email_assistant','router',    'Routes taxpayer emails to specialist agents by inquiry category'),
('rag-search-v1',      'rag_search',         'linear',    'Embeds query, retrieves pgvector chunks, generates grounded answer'),
('compliance-check-v1','graph_compliance',   'supervisor','Multi-agent compliance audit via Microsoft Graph API')
ON CONFLICT DO NOTHING;

-- ── Seed: default policies ────────────────────────────────────
INSERT INTO ao_policies (app_id, name, stage, action) VALUES
('tax_email_assistant','content_safety','pre_execution', 'block'),
('tax_email_assistant','pii_filter',    'pre_execution', 'warn'),
('tax_email_assistant','pii_filter',    'post_execution','redact'),
('tax_email_assistant','content_safety','post_execution','warn'),
('tax_email_assistant','tax_accuracy',  'post_execution','warn'),
('rag_search',         'pii_filter',    'pre_execution', 'redact'),
('graph_compliance',   'audit_log',     'runtime',       'log')
ON CONFLICT DO NOTHING;
