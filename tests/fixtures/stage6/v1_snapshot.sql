PRAGMA foreign_keys = ON;

CREATE TABLE snapshot_meta (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    scanner_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    extractor_versions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    repo_root_norm TEXT NOT NULL,
    git_head TEXT,
    git_branch TEXT,
    git_status_fingerprint TEXT NOT NULL,
    repo_state_before_json TEXT NOT NULL,
    repo_state_after_json TEXT NOT NULL,
    build_status TEXT NOT NULL,
    logical_fingerprint TEXT NOT NULL,
    observation_fingerprint TEXT NOT NULL
);

CREATE TABLE files (
    file_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    git_population TEXT NOT NULL,
    file_kind TEXT NOT NULL,
    language TEXT,
    extension TEXT NOT NULL,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    line_count INTEGER,
    encoding TEXT,
    content_hash TEXT,
    analysis_level TEXT NOT NULL,
    classification_reason TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    UNIQUE(snapshot_id, path_key)
);

CREATE TABLE pruned_roots (
    pruned_root_id INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_policy TEXT NOT NULL,
    UNIQUE(snapshot_id, relative_path)
);

CREATE TABLE symbols (
    symbol_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    qualified_name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    symbol_kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_column INTEGER NOT NULL,
    end_column INTEGER,
    parent_symbol_id TEXT REFERENCES symbols(symbol_id),
    signature_text TEXT,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);

CREATE TABLE imports (
    import_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    import_kind TEXT NOT NULL,
    module_text TEXT NOT NULL,
    imported_name TEXT,
    alias TEXT,
    relative_level INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    resolution_status TEXT NOT NULL,
    resolved_file_id TEXT REFERENCES files(file_id),
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);

CREATE TABLE relations (
    relation_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    relation_kind TEXT NOT NULL,
    source_file_id TEXT REFERENCES files(file_id),
    source_symbol_id TEXT REFERENCES symbols(symbol_id),
    target_file_id TEXT REFERENCES files(file_id),
    target_symbol_id TEXT REFERENCES symbols(symbol_id),
    target_text TEXT,
    start_line INTEGER,
    end_line INTEGER,
    start_column INTEGER,
    end_column INTEGER,
    resolution_status TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);

CREATE TABLE parse_diagnostics (
    diagnostic_id INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    diagnostic_kind TEXT NOT NULL,
    message TEXT NOT NULL,
    line INTEGER,
    column INTEGER,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);

CREATE TABLE index_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_code TEXT,
    candidate_count INTEGER NOT NULL,
    candidate_bytes INTEGER NOT NULL,
    metadata_only_count INTEGER NOT NULL,
    pruned_root_count INTEGER NOT NULL,
    text_file_count INTEGER NOT NULL,
    parsed_file_count INTEGER NOT NULL,
    parse_failure_count INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL,
    import_count INTEGER NOT NULL,
    relation_count INTEGER NOT NULL,
    enumeration_ms INTEGER NOT NULL,
    classification_ms INTEGER NOT NULL,
    read_hash_ms INTEGER NOT NULL,
    parse_ms INTEGER NOT NULL,
    relations_ms INTEGER NOT NULL,
    database_write_ms INTEGER NOT NULL,
    total_ms INTEGER NOT NULL,
    snapshot_size_bytes INTEGER NOT NULL,
    published_snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id),
    warnings_json TEXT NOT NULL
);

CREATE INDEX files_relative_path_idx ON files(relative_path);
CREATE INDEX symbols_short_name_idx ON symbols(short_name, qualified_name);
CREATE INDEX symbols_file_idx ON symbols(file_id, start_line);
CREATE INDEX imports_file_idx ON imports(file_id, start_line);
CREATE INDEX relations_kind_idx ON relations(relation_kind);
CREATE INDEX diagnostics_file_idx ON parse_diagnostics(file_id, line);
