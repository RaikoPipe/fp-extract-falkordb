"""Aggregate all tools into a single list."""

from falkordb_harness.tools.admin_tools import reset_graph, use_graph
from falkordb_harness.tools.conflict_tools import clear_conflicts, get_conflicts
from falkordb_harness.tools.file_inspection_tools import file_metadata, read_excerpt
from falkordb_harness.tools.ingest_tools import chunk_documents, extract_and_write
from falkordb_harness.tools.inspect_tools import (
    get_schema,
    list_edges,
    list_graphs,
    list_nodes,
    node_count,
)
from falkordb_harness.tools.preprocess_tools import preprocess_document
from falkordb_harness.tools.query_tools import (
    cypher_query,
    fulltext_search,
    nl_query,
    vector_search,
)
from falkordb_harness.tools.reconciliation_tools import (
    clear_reconciliations,
    get_reconciliations,
    reconcile_posthoc,
)

all_tools = [
    # File inspection (use before ingestion — see PRE-INGESTION REVIEW ROUTINE)
    file_metadata,
    read_excerpt,
    # Preprocessing (raw source -> Markdown in PREPROCESSED_DIR)
    preprocess_document,
    # Ingestion
    chunk_documents,
    extract_and_write,
    # Query
    cypher_query,
    nl_query,
    fulltext_search,
    vector_search,
    # Inspection
    get_schema,
    list_nodes,
    list_edges,
    node_count,
    list_graphs,
    # Conflicts
    get_conflicts,
    clear_conflicts,
    # Reconciliation
    get_reconciliations,
    clear_reconciliations,
    reconcile_posthoc,
    # Admin
    reset_graph,
    use_graph,
]
